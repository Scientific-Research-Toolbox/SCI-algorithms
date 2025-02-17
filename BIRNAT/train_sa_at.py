# training model with self attention and adversial training
from dataLoadess import OrigTrainDataset
from torch.utils.data import DataLoader
from models import forward_rnn, cnn1, backrnn             # with attention
from gan_resnet import Discriminator
from utils import generate_masks, time2file_name,save_test_result
import torch.optim as optim
import torch.nn as nn
import torch
import scipy.io as scio
import time
import datetime
import os
import logging
import numpy as np
from torch.autograd import Variable
from skimage.metrics import structural_similarity as ssim
from torch import autograd
from torch.nn import functional as F
from os.path import join as opj
import cv2

### environ
os.environ["CUDA_VISIBLE_DEVICES"] = "0"
if not torch.cuda.is_available():
    raise Exception('NO GPU!')

### setting
## path
#train_data_path = "/data/zzh/project/E2E_CNN/data_simu/training_truth/data_augment_256_10f"  # traning data from DAVIS2017
mask_path = "/data/zzh/project/RNN_SCI/Data/data_simu/exp_mask"
test_path = '/data/zzh/project/RNN_SCI/Data/data_simu/testing_truth/bm_256_10f/'   # simulation benchmark data for comparison
train_data_path =test_path # for test

## param
pretrained_model = ''
mask_name = 'multiplex_shift_binary_mask_256_10f.mat'
Cr = 10
block_size = 256
last_train = 0
max_iter = 100
batch_size = 1
learning_rate = 0.0003
lr_decay = 0.95
lr_decay_step = 3   # epoch interval for learning rate decay
checkpoint_step = 5 # epoch interval for save checkpoints
mode = 'train'  # train or test


## data set
mask, mask_s = generate_masks(mask_path, mask_name)
dataset = OrigTrainDataset(train_data_path, mask_path+'/'+mask_name)

train_data_loader = DataLoader(dataset=dataset, batch_size=batch_size, shuffle=True)


## model set
first_frame_net = cnn1(Cr+1).cuda()
rnn1 = forward_rnn().cuda()
rnn2 = backrnn().cuda()
D = Discriminator(1, 256, Cr, 64, 1024)
D.cuda()

if last_train != 0:
    first_frame_net = torch.load(
        './model/' + pretrained_model + "/first_frame_net_model_epoch_{}.pth".format(last_train))
    rnn1 = torch.load('./model/' + pretrained_model + "/rnn1_model_epoch_{}.pth".format(last_train))
    rnn2 = torch.load('./model/' + pretrained_model + "/rnn2_model_epoch_{}.pth".format(last_train))
    print('pre-trained model: \'{} - No. {} epoch\' loaded!'.format(pretrained_model, last_train))
    
loss = nn.MSELoss()
loss.cuda()
BCE_loss = nn.BCELoss().cuda()


### function
## test
def test(test_path, epoch, result_path, logger):
    test_list = os.listdir(test_path)
    psnr_forward = torch.zeros(len(test_list))
    psnr_backward = torch.zeros(len(test_list))
    ssim_forward = torch.zeros(len(test_list))
    ssim_backward = torch.zeros(len(test_list))

    # load test data
    for i in range(len(test_list)):
        # load orig pic
        pic = scio.loadmat(test_path + '/' + test_list[i])

        if "orig" in pic:
            pic = pic['orig']
        else:
            raise KeyError("KEY 'orig' is not in the variable")
        pic = pic / 255

        # calc meas
        pic_gt = np.zeros([pic.shape[2] // Cr, Cr, block_size, block_size])
        for jj in range(pic.shape[2] // Cr*Cr):
            if jj % Cr == 0:
                meas_t = np.zeros([block_size, block_size])
                n = 0
            pic_t = pic[:, :, jj]
            mask_t = mask[n, :, :]

            mask_t = mask_t.cpu()
            pic_gt[jj // Cr, n, :, :] = pic_t
            n += 1
            meas_t = meas_t + np.multiply(mask_t.numpy(), pic_t)

            if jj == Cr-1:
                meas_t = np.expand_dims(meas_t, 0)
                meas = meas_t
            elif (jj + 1) % Cr == 0: #zzh
                meas_t = np.expand_dims(meas_t, 0)
                meas = np.concatenate((meas, meas_t), axis=0)
        
        # calc
        meas = torch.from_numpy(meas)
        pic_gt = torch.from_numpy(pic_gt)
        meas = meas.cuda()
        pic_gt = pic_gt.cuda()
        meas = meas.float()
        pic_gt = pic_gt.float()

        meas_re = torch.div(meas, mask_s)
        meas_re = torch.unsqueeze(meas_re, 1)
        
        with torch.no_grad():
            h0 = torch.zeros(meas.shape[0], 20, block_size, block_size).cuda()
            xt1 = first_frame_net(mask, meas_re, block_size, Cr)
            out_pic1,h1 = rnn1(xt1, meas, mask, h0, meas_re, block_size, Cr)
            out_pic2 = rnn2(out_pic1, meas, mask, h1, meas_re, block_size, Cr)        #  out_pic1[:, fn-1, :, :]
        
        # calculate psnr and ssim
            psnr_1 = 0
            psnr_2 = 0
            ssim_1 = 0
            ssim_2 = 0

            for ii in range(meas.shape[0] * Cr):
                out_pic_forward = out_pic1[ii // Cr, ii % Cr, :, :]
                out_pic_backward = out_pic2[ii // Cr, ii % Cr, :, :]
                gt_t = pic_gt[ii // Cr, ii % Cr, :, :]
                mse_forward = loss(out_pic_forward * 255, gt_t * 255)
                mse_forward = mse_forward.data
                mse_backward = loss(out_pic_backward * 255, gt_t * 255)
                mse_backward = mse_backward.data
                psnr_1 += 10 * torch.log10(255 * 255 / mse_forward)
                psnr_2 += 10 * torch.log10(255 * 255 / mse_backward)

                ssim_1 += ssim(out_pic_forward.cpu().numpy(), gt_t.cpu().numpy())
                ssim_2 += ssim(out_pic_backward.cpu().numpy(), gt_t.cpu().numpy())

            psnr_1 = psnr_1 / (meas.shape[0] * Cr)
            psnr_2 = psnr_2 / (meas.shape[0] * Cr)
            psnr_forward[i] = psnr_1
            psnr_backward[i] = psnr_2

            ssim_1 = ssim_1 / (meas.shape[0] * Cr)
            ssim_2 = ssim_2 / (meas.shape[0] * Cr)
            ssim_forward[i] = ssim_1
            ssim_backward[i] = ssim_2

            # save test result
            if epoch % 5 == 0 or (epoch > 50 and epoch % 2 == 0):                
                save_test_result(result_path,test_list[i],epoch,out_pic2,pic_gt,psnr_2, ssim_2,block_size)
                
    logger.info("only forward rnn result (psnr/ssim): {:.4f}/{:.4f}   backward rnn result: {:.4f}/{:.4f}"\
        .format(torch.mean(psnr_forward), torch.mean(ssim_forward), torch.mean(psnr_backward), torch.mean(ssim_backward)))

## train
def train(epoch, learning_rate, result_path, logger):
    epoch_loss = 0
    epoch_loss1 = 0
    epoch_loss2 = 0
    Dloss = 0
    regloss = 0
        
    optimizer_g = optim.Adam([{'params': first_frame_net.parameters()}, {'params': rnn1.parameters()},
                            {'params': rnn2.parameters()}], lr=learning_rate)
    optimizer_d = optim.Adam(D.parameters(), lr=learning_rate)

    
    # if __name__ == '__main__':
    for iteration, batch in enumerate(train_data_loader):
        gt, meas = Variable(batch[0]), Variable(batch[1])
        gt = gt.cuda()  # [batch,Cr,block_size,block_size]
        gt = gt.float()
        meas = meas.cuda()  # [batch,block_size block_size]
        meas = meas.float()

        mini_batch = gt.size()[0]
        y_real_ = torch.ones(mini_batch).cuda()
        y_fake_ = torch.zeros(mini_batch).cuda()

        meas_re = torch.div(meas, mask_s)
        meas_re = torch.unsqueeze(meas_re, 1)

        optimizer_d.zero_grad()

        batch_size1 = gt.shape[0]
        # print(meas.shape,gt.shape) #zzh debug
        # Cr = gt.shape[1]
        
        h0 = torch.zeros(batch_size1, 20, block_size, block_size).cuda()
        xt1 = first_frame_net(mask, meas_re, block_size, Cr)
        model_out1, h1 = rnn1(xt1, meas, mask, h0, meas_re, block_size, Cr)
        model_out = rnn2(model_out1, meas, mask, h1,meas_re, block_size, Cr)           #  model_out1[:, fn-1, :, :]
        
        # discriminator training
        toggle_grad(first_frame_net, False)
        toggle_grad(rnn1, False)
        toggle_grad(rnn2, False)
        toggle_grad(D, True)
        gt.requires_grad_()

        D_result = D(gt, y_real_)
        # assert (D_result > 0.0 & D_result < 1.0).all()
        D_real_loss = compute_loss(D_result, 1)
        Dloss += D_result.data.mean()
        D_real_loss.backward(retain_graph=True)

        # model_out.requires_grad_()
        # d_fake = D(model_out, y_real_)
        # dloss_fake = compute_loss(d_fake, 0)

        batch_size = gt.size(0)
        grad_dout = autograd.grad(
            outputs=D_result.sum(), inputs=gt,
            create_graph=True, retain_graph=True, only_inputs=True
        )[0]
        grad_dout2 = grad_dout.pow(2)
        assert (grad_dout2.size() == gt.size())
        reg1 = grad_dout2.view(batch_size, -1).sum(1)

        reg = 10 * reg1.mean()

        regloss += reg.data.mean()

        reg.backward(retain_graph=True)

        optimizer_d.step()

        # generator training
        toggle_grad(first_frame_net, True)
        toggle_grad(rnn1, True)
        toggle_grad(rnn2, True)
        toggle_grad(D, False)
        optimizer_g.zero_grad()

        D_result = D(model_out, y_real_)
        G_train_loss = compute_loss(D_result, 1)
        Loss1 = loss(model_out1, gt)
        Loss2 = loss(model_out, gt)
        Loss = 0.5 * Loss1 + 0.5 * Loss2 + 0.001 * G_train_loss

        epoch_loss += Loss.data
        epoch_loss1 += Loss1.data
        epoch_loss2 += Loss2.data

        Loss.backward()
        optimizer_g.step()

    test(test_path1, epoch, result_path)

    end = time.time()
    logger.info("===> Epoch {} Complete: Avg. Loss: {:.7f}".format(epoch, epoch_loss / len(train_data_loader)),
          "loss1 {:.7f} loss2: {:.7f}".format(epoch_loss1 / len(train_data_loader),
                                              epoch_loss2 / len(train_data_loader)),
          "d loss: {:.7f},reg loss: {:.7f}".format(Dloss / len(train_data_loader),
                                                   regloss / len(train_data_loader)),
          "  time: {:.2f}".format(end - begin))


def compute_loss(d_out, target):
    targets = d_out.new_full(size=d_out.size(), fill_value=target)
    loss = F.binary_cross_entropy_with_logits(d_out, targets)

    return loss

def toggle_grad(model, requires_grad):
    for p in model.parameters():
        p.requires_grad_(requires_grad)

def checkpointD(epoch, model_path):
    model_out_path = './' + model_path + '/' + "discriminator_model_epoch_{}.pth".format(epoch)
    torch.save(D, model_out_path)
    # print("Checkpoint saved to {}".format(model_out_path))

## checkpoint
def checkpoint(epoch, model_path, logger):
    model_out_path = './' + model_path + '/' + "first_frame_net_model_epoch_{}.pth".format(epoch)
    torch.save(first_frame_net, model_out_path)
    logger.info("Checkpoint saved to {}".format(model_out_path))


def checkpoint2(epoch, model_path):
    model_out_path = './' + model_path + '/' + "rnn1_model_epoch_{}.pth".format(epoch)
    torch.save(rnn1, model_out_path)
    # print("Checkpoint saved to {}".format(model_out_path))


def checkpoint3(epoch, model_path):
    model_out_path = './' + model_path + '/' + "rnn2_model_epoch_{}.pth".format(epoch)
    torch.save(rnn2, model_out_path)
    # print("Checkpoint saved to {}".format(model_out_path))


def main(learning_rate):
    # prepare
    date_time = str(datetime.datetime.now())
    date_time = time2file_name(date_time)
    
    result_path = 'recon' + '/' + date_time
    model_path = 'model' + '/' + date_time
    if not os.path.exists(result_path):
        os.makedirs(result_path)
    if not os.path.exists(model_path):
        os.makedirs(model_path)


    # logging
    logger = logging.getLogger()
    logger.setLevel(logging.INFO) 
    formatter = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
    
    log_file = model_path + '/log.txt'
    fh = logging.FileHandler(log_file, mode='a')
    fh.setLevel(logging.INFO) 
    fh.setFormatter(formatter)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    
    # train
    print('\n---- start training ----\n')
    logger.info('Code: train_at.py') 
    logger.info('mask: {}'.format(mask_path + '/' + mask_name)) 
    if last_train != 0:
        logger.info('loading pre-trained model: \'{} - No. {} epoch\'...'.format(pretrained_model, last_train))
            
    for epoch in range(last_train + 1, last_train + max_iter + 1):
        train(epoch, learning_rate, result_path, logger)
        if (epoch % checkpoint_step == 0 or epoch > 70):
            checkpoint(epoch, model_path, logger)
            checkpoint2(epoch, model_path)
            checkpoint3(epoch, model_path)
            checkpointD(epoch, model_path)
        if (epoch % lr_decay_step == 0) and (epoch < 150):
            learning_rate = learning_rate * lr_decay
            logger.info('current learning rate: {}\n'.format(learning_rate))


if __name__ == '__main__':
    begin = time.time()
    main(learning_rate)
