
import datetime
import os
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Function
import torchvision.utils as vutils
from tensorboardX import SummaryWriter

from networks.blocks import UNetConvBlock2D, UNetUpSamplingBlock2D
from networks.cnn import CNN

# gradient reversal
class ReverseLayerF(Function):

    @staticmethod
    def forward(ctx, x):
        return x.view_as(x)

    @staticmethod
    def backward(ctx, grad_output):
        output = grad_output.neg()

        return output, None

# original 2D unet encoder
class UNetEncoder(nn.Module):

    def __init__(self, in_channels=1, feature_maps=64, levels=4, group_norm=False):
        super(UNetEncoder, self).__init__()

        self.in_channels = in_channels
        self.feature_maps = feature_maps
        self.levels = levels
        self.features = nn.Sequential()

        in_features = in_channels
        for i in range(levels):
            out_features = (2**i) * feature_maps

            # convolutional block
            conv_block = UNetConvBlock2D(in_features, out_features, group_norm=group_norm)
            self.features.add_module('convblock%d' % (i + 1), conv_block)

            # pooling
            pool = nn.MaxPool2d(kernel_size=2, stride=2)
            self.features.add_module('pool%d' % (i + 1), pool)

            # input features for next block
            in_features = out_features

        # center (lowest) block
        self.center_conv = UNetConvBlock2D(2**(levels-1) * feature_maps, 2**levels * feature_maps, group_norm=group_norm)

    def forward(self, inputs):

        encoder_outputs = []  # for decoder skip connections

        outputs = inputs
        for i in range(self.levels):
            outputs = getattr(self.features,'convblock%d' % (i + 1))(outputs)
            encoder_outputs.append(outputs)
            outputs = getattr(self.features,'pool%d' % (i + 1))(outputs)

        outputs = self.center_conv(outputs)
        reverse_outputs = ReverseLayerF.apply(outputs)

        return encoder_outputs, outputs, reverse_outputs

# original 2D unet decoder
class UNetDecoder(nn.Module):

    def __init__(self, in_channels=1, out_channels=2, feature_maps=64, levels=4, skip_connections=True, group_norm=False):
        super(UNetDecoder, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.feature_maps = feature_maps
        self.levels = levels
        self.skip_connections = skip_connections
        self.features = nn.Sequential()

        for i in range(levels):

            # upsampling block
            upconv = UNetUpSamplingBlock2D(2**(levels-i) * feature_maps, 2**(levels-i-1) * feature_maps, deconv=True)
            self.features.add_module('upconv%d' % (i + 1), upconv)

            # convolutional block
            if skip_connections:
                conv_block = UNetConvBlock2D(2**(levels-i) * feature_maps, 2**(levels-i-1) * feature_maps, group_norm=group_norm)
            else:
                conv_block = UNetConvBlock2D(2**(levels-i-1) * feature_maps, 2**(levels-i-1) * feature_maps, group_norm=group_norm)
            self.features.add_module('convblock%d' % (i + 1), conv_block)

        # output layer
        self.output = nn.Conv2d(feature_maps, out_channels, kernel_size=1)

    def forward(self, inputs, encoder_outputs):

        decoder_outputs = []

        encoder_outputs.reverse()

        outputs = inputs
        for i in range(self.levels):
            if self.skip_connections:
                outputs = getattr(self.features, 'upconv%d' % (i + 1))(encoder_outputs[i], outputs)  # also deals with concat
            else:
                outputs = getattr(self.features, 'upconv%d' % (i + 1))(outputs)  # no concat
            outputs = getattr(self.features, 'convblock%d' % (i + 1))(outputs)
            decoder_outputs.append(outputs)

        outputs = self.output(outputs)

        return decoder_outputs, outputs

# original 2D unet model
class UNetAE(nn.Module):

    def __init__(self, in_channels=1, out_channels=1, feature_maps=64, levels=4, group_norm=False, lambda_rec=0, s=128):
        super(UNetAE, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.feature_maps = feature_maps
        self.levels = levels
        self.group_norm = group_norm
        self.lambda_rec = lambda_rec

        # contractive path
        self.encoder = UNetEncoder(in_channels, feature_maps=feature_maps, levels=levels, group_norm=group_norm)
        # expansive path
        self.decoder = UNetDecoder(in_channels, out_channels, feature_maps=feature_maps, levels=levels, skip_connections=False, group_norm=group_norm)

        # domain classifier on the latent encoding (fixed network architecture)
        n = s//(2**levels)
        fm = feature_maps*(2**levels)
        conv_channels = [48,48,48]
        fc_channels = [48, 24, 2]
        self.domain_classifier = CNN(input_size=(fm, n, n), conv_channels=conv_channels, fc_channels=fc_channels)


    def forward(self, inputs):

        # contractive path
        encoder_outputs, final_output, reverse_outputs = self.encoder(inputs)

        # domain prediction
        domain = self.domain_classifier(reverse_outputs)

        # expansive path
        decoder_outputs, outputs = self.decoder(final_output, encoder_outputs)

        return outputs, domain

    # trains the network for one epoch
    def train_epoch(self, src_loader, tar_loader, loss_fn, optimizer, epoch, print_stats=1, writer=None, write_images=False):

        # make sure network is on the gpu and in training mode
        self.cuda()
        self.train()

        # domain loss function
        loss_dom_fn = nn.CrossEntropyLoss()

        # keep track of the average loss during the epoch
        loss_rec_cum = 0.0
        loss_dom_cum = 0.0
        loss_cum = 0.0
        cnt = 0

        # start epoch
        tar_data = list(enumerate(tar_loader))
        for i, data in enumerate(src_loader):

            # get the inputs
            x_src = data.cuda()
            x_tar = tar_data[i][1].cuda()
            x = torch.cat((x_src, x_tar), dim=0)

            # prep domain labels
            dom_src = torch.zeros((x_src.size(0))).cuda().long()
            dom_tar = torch.ones((x_tar.size(0))).cuda().long()
            dom = torch.cat((dom_src, dom_tar), dim=0)

            # zero the gradient buffers
            self.zero_grad()

            # forward prop
            y_pred, dom_pred = self(x)

            # compute loss
            loss_rec = loss_fn(y_pred, x)
            loss_dom = loss_dom_fn(dom_pred, dom)
            loss = loss_rec + self.lambda_rec*loss_dom
            loss_rec_cum += loss_rec.data.cpu().numpy()
            loss_dom_cum += loss_dom.data.cpu().numpy()
            loss_cum += loss.data.cpu().numpy()
            cnt += 1

            # backward prop
            loss.backward()

            # apply one step in the optimization
            optimizer.step()

            # print statistics if necessary
            if i % print_stats == 0:
                print('[%s] Epoch %5d - Iteration %5d/%5d - Loss: %.6f'
                      % (datetime.datetime.now(), epoch, i, len(src_loader.dataset)/src_loader.batch_size, loss))

        # don't forget to compute the average and print it
        loss_rec_avg = loss_rec_cum / cnt
        loss_dom_avg = loss_dom_cum / cnt
        loss_avg = loss_cum / cnt
        print('[%s] Epoch %5d - Average train loss: %.6f'
              % (datetime.datetime.now(), epoch, loss_avg))

        # log everything
        if writer is not None:

            # always log scalars
            writer.add_scalar('train/loss-rec', loss_rec_avg, epoch)
            writer.add_scalar('train/loss-dom', loss_dom_avg, epoch)
            writer.add_scalar('train/loss', loss_avg, epoch)

            if write_images:
                # write images
                x = vutils.make_grid(x, normalize=True, scale_each=True)
                x_rec = vutils.make_grid(torch.sigmoid(y_pred).data, normalize=True, scale_each=True)
                writer.add_image('train/x-rec-input', x, epoch)
                writer.add_image('train/x-rec-output', x_rec, epoch)

        return loss_avg

    # tests the network over one epoch
    def test_epoch(self, src_loader, tar_loader, loss_fn, epoch, writer=None, write_images=False):

        # make sure network is on the gpu and in training mode
        self.cuda()
        self.eval()

        # domain loss function
        loss_dom_fn = nn.CrossEntropyLoss()

        # keep track of the average loss and metrics during the epoch
        loss_rec_cum = 0.0
        loss_dom_cum = 0.0
        loss_cum = 0.0
        cnt = 0

        # test loss
        tar_data = list(enumerate(tar_loader))
        for i, data in enumerate(src_loader):

            # get the inputs
            x_src = data.cuda()
            x_tar = tar_data[i][1].cuda()
            x = torch.cat((x_src, x_tar), dim=0)

            # prep domain labels
            dom_src = torch.zeros((x_src.size(0))).cuda().long()
            dom_tar = torch.ones((x_tar.size(0))).cuda().long()
            dom = torch.cat((dom_src, dom_tar), dim=0)

            # forward prop
            y_pred, dom_pred = self(x)

            # compute loss
            loss_rec = loss_fn(y_pred, x)
            loss_dom = loss_dom_fn(dom_pred, dom)
            loss = loss_rec + self.lambda_rec*loss_dom
            loss_rec_cum += loss_rec.data.cpu().numpy()
            loss_dom_cum += loss_dom.data.cpu().numpy()
            loss_cum += loss.data.cpu().numpy()
            cnt += 1

        # don't forget to compute the average and print it
        loss_rec_avg = loss_rec_cum / cnt
        loss_dom_avg = loss_dom_cum / cnt
        loss_avg = loss_cum / cnt
        print('[%s] Epoch %5d - Average test loss: %.6f'
              % (datetime.datetime.now(), epoch, loss_avg))

        # log everything
        if writer is not None:

            # always log scalars
            writer.add_scalar('test/loss-rec', loss_rec_avg, epoch)
            writer.add_scalar('test/loss-dom', loss_dom_avg, epoch)
            writer.add_scalar('test/loss', loss_avg, epoch)

            if write_images:
                x = vutils.make_grid(x, normalize=True, scale_each=True)
                x_rec = vutils.make_grid(torch.sigmoid(y_pred).data, normalize=True, scale_each=True)
                writer.add_image('test/x-rec-input', x, epoch)
                writer.add_image('test/x-rec-output', x_rec, epoch)

        return loss_avg

    # trains the network
    def train_net(self, src_train_loader, src_test_loader, tar_train_loader, tar_test_loader, loss_fn, lr=1e-3, step_size=1, gamma=1, epochs=100, test_freq=1, print_stats=1, log_dir=None, write_images_freq=1):

        # log everything if necessary
        if log_dir is not None:
            writer = SummaryWriter(log_dir=log_dir)
        else:
            writer = None

        optimizer = optim.Adam(self.parameters(), lr=lr)
        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=step_size, gamma=gamma)

        test_loss_min = np.inf
        for epoch in range(epochs):

            print('[%s] Epoch %5d/%5d' % (datetime.datetime.now(), epoch, epochs))

            # train the model for one epoch
            self.train_epoch(src_loader=src_train_loader, tar_loader=tar_train_loader, loss_fn=loss_fn, optimizer=optimizer, epoch=epoch,
                             print_stats=print_stats, writer=writer, write_images=epoch % write_images_freq == 0)

            # adjust learning rate if necessary
            if scheduler is not None:
                scheduler.step(epoch=epoch)

                # and keep track of the learning rate
                writer.add_scalar('learning_rate', float(scheduler.get_lr()[0]), epoch)

            # test the model for one epoch is necessary
            if epoch % test_freq == 0:
                test_loss = self.test_epoch(src_loader=src_test_loader, tar_loader=tar_test_loader, loss_fn=loss_fn,
                                            epoch=epoch, writer=writer, write_images=True)

                # and save model if lower test loss is found
                if test_loss < test_loss_min:
                    test_loss_min = test_loss
                    torch.save(self, os.path.join(log_dir, 'best_checkpoint.pytorch'))

            # save model every epoch
            torch.save(self, os.path.join(log_dir, 'checkpoint.pytorch'))

        writer.close()