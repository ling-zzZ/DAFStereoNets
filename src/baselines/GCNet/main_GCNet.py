# !/usr/bin/env python3
# -*-coding:utf-8-*-
# @file: main_GCNet.py
# @brief:
# @author: Changjiang Cai, ccai1@stevens.edu, caicj5351@gmail.com
# @version: 0.0.1
# @creation date: 07-01-2020
# @last modified: Thu 09 Apr 2020 02:21:58 PM EDT

from __future__ import print_function

import sys
import shutil
import os
from os.path import join as pjoin
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.optim as optim
import cv2
from torch.utils.data import DataLoader
import torch.nn.functional as F

#from .loaddata.data import get_training_set, get_valid_set, load_test_data, test_transform
from src.loaddata.data import get_training_set, load_test_data, test_transform
from src.loaddata.dataset import get_virtual_kitti2_filelist

from torch.utils.tensorboard import SummaryWriter
from src.dispColor import colormap_jet_batch_image,KT15FalseColorDisp,KT15LogColorDispErr

#from src.utils import writeKT15FalseColors # this is numpy fuction, it is SO SLOW !!!
# this is cython fuction, it is SO QUICK !!!
from src.cython import writeKT15FalseColor as KT15FalseClr
from src.cython import writeKT15ErrorLogColor as KT15LogClr
import numpy as np
import src.pfmutil as pfm
import time
from .models.loss import valid_accu3, MyLoss2

from .models.gcnet import GCNet
from datetime import datetime


#added by CCJ:
def get_epe_rate2(disp, prediction, max_disp = 192, threshold = 1.0):
    mask = np.logical_and(disp >= 0.001, disp <= max_disp)
    error = np.mean(np.abs(prediction[mask] - disp[mask]))
    rate = np.sum(np.abs(prediction[mask] - disp[mask]) > threshold) / np.sum(mask)
    #print(" ==> EPE Error: {:.4f}, Error Rate: {:.4f}".format(error, rate))
    return error, rate

def get_epe_rate(disp, prediction, threshold = 1.0, threshold2 = 3.0):
    #mask = np.logical_and(disp >= 0.001, disp <= max_disp)
    mask = disp >= 0.001
    error = np.mean(np.abs(prediction[mask] - disp[mask]))
    rate = np.sum(np.abs(prediction[mask] - disp[mask]) > threshold) / np.sum(mask)
    rate2 = np.sum(np.abs(prediction[mask] - disp[mask]) > threshold2) / np.sum(mask)
    #print(" ==> EPE Error: {:.4f}, Error Rate: {:.4f}".format(error, rate))
    return error, rate, rate2

""" train and test GCNet """
class MyGCNet(object):
    def __init__(self, args):
        self.args = args
        self.model_name = args.model_name
        self.lr = args.lr
        self.kitti2012  = args.kitti2012
        self.kitti2015  = args.kitti2015
        self.virtual_kitti2 = args.virtual_kitti2
        self.checkpoint_dir = args.checkpoint_dir
        self.log_summary_step = args.log_summary_step
        self.isTestingMode = (str(args.mode).lower() == 'test')
        self.cuda = args.cuda
        self.kt12_image_mode = str(args.kt12_image_mode).lower()
        self.is_data_augment = str(args.is_data_augment).lower() == 'true'
        # I find complicated data_augment is not helpful for GCNet;
        assert self.is_data_augment == False
        print ("[***] is_data_augment = ", self.is_data_augment)
        print ("[***] kt12_image_mode = ", self.kt12_image_mode)
        if not self.isTestingMode: # training mode
            print('===> Loading datasets')
            train_set = get_training_set(args.data_path, args.training_list, 
                    [args.crop_height, args.crop_width], 
                    args.kitti2012, args.kitti2015, args.virtual_kitti2,
                    args.shift, False,# is_semantic
                    self.kt12_image_mode,
                    self.is_data_augment
                    )
            
            self.training_data_loader = DataLoader(dataset=train_set, 
                    num_workers=args.threads, batch_size=args.batchSize, 
                    shuffle=True, drop_last=True)
            
            self.train_loader_len = len(self.training_data_loader)
            self.criterion = MyLoss2(thresh=3, alpha=2)
            
            if not os.path.exists(args.checkpoint_dir):
                os.makedirs(args.checkpoint_dir)

        
        print('===> Building GCNet Model')
        self.model = GCNet(
                args.max_disp, 
                is_kendall_version = str(args.is_kendall_version).lower() == 'true',
                is_quarter_size_cost_volume_gcnet = str(args.is_quarter_size_cost_volume_gcnet).lower() == 'true'
                ) 
        print('[***]Number of model parameters: {}'.format(sum([p.data.nelement() for p in self.model.parameters()])))
        #sys.exit()

        if self.cuda:
            self.model = torch.nn.DataParallel(self.model).cuda()
        
        if not self.isTestingMode: # training mode
            """ We need to set requires_grad == False to freeze the parameters 
                so that the gradients are not computed in backward();
                Parameters of newly constructed modules have requires_grad=True by default;
            """
            # updated for the cases where some subnetwork was forzen!!!
            params_to_update = [p for p in self.model.parameters() if p.requires_grad]
            if 0:
                print ('[****] params_to_update = ')
                for p in params_to_update:
                    print (type(p.data), p.size())

            #self.optimizer= optim.Adam(params_to_update, lr = args.lr, betas=(0.9,0.999))
            self.optimizer= optim.RMSprop(params_to_update, lr = args.lr, alpha=0.9)
            self.writer = SummaryWriter(args.train_logdir)
        

        
        if self.isTestingMode:
            assert os.path.isfile(args.resume) == True, "Model Test but NO checkpoint found at {}".format(args.resume)
        if args.resume:
            if os.path.isfile(args.resume):
                print("[***] => loading checkpoint '{}'".format(args.resume))
                checkpoint = torch.load(args.resume)
                self.model.load_state_dict(checkpoint['state_dict'], strict=False)
                if not self.isTestingMode: # training mode
                    self.optimizer.load_state_dict(checkpoint['optimizer'])
            else:
                print("=> no checkpoint found at {}".format(args.resume))
        
    
    def save_checkpoint(self, epoch, state_dict, is_best=False):
        saved_checkpts = pjoin(self.checkpoint_dir, self.model_name)
        if not os.path.exists(saved_checkpts):
            os.makedirs(saved_checkpts)
            print ('makedirs {}'.format(saved_checkpts))
        
        filename = pjoin(saved_checkpts, "model_epoch_%05d.tar" % epoch)
        torch.save(state_dict, filename)
        print ('Saved checkpoint at %s' % filename) 
        if is_best:
            best_fname = pjoin(saved_checkpts, 'model_best.tar')
            shutil.copyfile(filename, best_fname)

    def adjust_learning_rate(self, epoch):
        if epoch <= 200:
            self.lr = self.args.lr
        else:
            self.lr = self.args.lr * 0.1
        
        print('learning rate = ', self.lr)
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = self.lr

    def load_checkpts(self, saved_checkpts = ''):
        print(" [*] Reading checkpoint %s" % saved_checkpts)
        
        checkpoint = None
        if saved_checkpts and saved_checkpts != '':
            try: #Exception Handling
                f = open(saved_checkpts, 'rb')
            except IsADirectoryError as error:
                print (error)
            else:
                checkpoint = torch.load(saved_checkpts)
        return checkpoint

    def build_train_summaries(self, imgl, imgr, disp, disp_gt, global_step, loss, epe_err, is_KT15Color = False, imgl_aug = None, imgr_aug = None):
            """ loss and epe error """
            self.writer.add_scalar(tag = 'train_loss', scalar_value = loss, global_step = global_step)
            self.writer.add_scalar(tag = 'train_err', scalar_value = epe_err, global_step = global_step)
            """ Add batched image data to summary:
                Note: add_images(img_tensor): img_tensor could be torch.Tensor, numpy.array, or string/blobname;
                so we could use torch.Tensor or numpy.array !!!
            """
            self.writer.add_images(tag='train_imgl',img_tensor=imgl, global_step = global_step, dataformats='NCHW')
            if imgr is not None:
                self.writer.add_images(tag='train_imgr',img_tensor=imgr, global_step = global_step, dataformats='NCHW')
            if imgl_aug is not None:
                self.writer.add_images(tag='train_imgl_aug',img_tensor=imgl_aug, global_step = global_step, dataformats='NCHW')
            if imgr_aug is not None:
                self.writer.add_images(tag='train_imgr_aug',img_tensor=imgr_aug, global_step = global_step, dataformats='NCHW')
            
            with torch.set_grad_enabled(False):
                if is_KT15Color:
                    disp_tmp = KT15FalseColorDisp(disp)
                    disp_gt_tmp = KT15FalseColorDisp(disp_gt)
                else:
                    disp_tmp = colormap_jet_batch_image(disp)
                    disp_gt_tmp = colormap_jet_batch_image(disp_gt)

                self.writer.add_images(tag='train_disp', img_tensor=disp_tmp, global_step = global_step, dataformats='NHWC')
                self.writer.add_images(tag='train_dispGT',img_tensor=disp_gt_tmp, global_step = global_step, dataformats='NHWC')
                self.writer.add_images(tag='train_dispErr',img_tensor=KT15LogColorDispErr(disp, disp_gt), 
                                       global_step = global_step, dataformats='NHWC')
    

    #---------------------
    #---- Training -------
    #---------------------
    def train(self, epoch):
        """Set up TensorBoard """
        epoch_loss = 0
        epoch_epe = 0
        epoch_accu3 = 0
        valid_iteration = 0

        #for iteration, batch_data in enumerate(self.training_data_loader):
        #    print (" [***] iteration = %d/%d" % (iteration, self.train_loader_len))
        #    input1 = batch_data[0].float() # False by default;
        #    input2 = batch_data[1].float()
        #    target = batch_data[2].float()
        #    left_rgb = batch_data[3].float()
        #sys.exit()
        
        # setting to train mode;
        self.model.train()
        self.adjust_learning_rate(epoch)

        """ running log loss """
        log_running_loss = 0.0
        log_running_err  = 0.0

        if self.kitti2012 or self.kitti2015:
            accu_thred = 3.0
        else:
            accu_thred = 1.0
        
        for iteration, batch_data in enumerate(self.training_data_loader):
            start = time.time()
            #print (" [***] iteration = %d" % iteration)
            input1 = batch_data[0].float() # False by default;
            #print ("[???] input1 require_grad = ", input1.requires_grad) # False
            input2 = batch_data[1].float()
            target = batch_data[2].float()
            left_rgb = batch_data[3].float()
            #right_rgb = batch_data[4].float()
            if self.is_data_augment:
                imgl_aug = input1
                #imgr_aug = input2
            else:
                imgl_aug = None
                #imgr_aug = None
            
            if self.cuda:
                input1 = input1.cuda()
                input2 = input2.cuda()
                target = target.cuda()

            target = torch.squeeze(target,1)
            # valid pixels: 0 < disparity < max_disp
            mask = (target - args.max_disp)*target < 0
            mask.detach_()
            valid_disp = target[mask].size()[0]
            
            if valid_disp > 0:
                self.optimizer.zero_grad()
                
                disp = self.model(input1, input2)
                loss0 = F.smooth_l1_loss(disp[mask], target[mask], reduction='mean')
                if self.kitti2012 or self.kitti2015:
                    loss = 0.4*loss0 + 0.6*self.criterion(disp[mask], target[mask])
                else:
                    loss = loss0
                
                loss.backward()
                self.optimizer.step()
                # MAE error
                error = torch.mean(torch.abs(disp[mask] - target[mask]))
                # accu3 
                accu = valid_accu3(target[mask], disp[mask], thred=accu_thred)

                epoch_loss += loss.item()
                epoch_epe += error.item()
                epoch_accu3 += accu.item() 
                valid_iteration += 1
                
                # epoch - 1: here argument `epoch` is starting from 1, instead of 0 (zer0);
                train_global_step = (epoch-1)*self.train_loader_len + iteration      
                print("===> Epoch[{}]({}/{}): Step {}, Loss: {:.3f}, EPE: {:.2f}, Acu{:.1f}: {:.2f}; {:.2f} s/step".format(
                                epoch, iteration, self.train_loader_len, train_global_step,
                                loss.item(), error.item(), accu_thred, accu.item(), time.time() -start))
                sys.stdout.flush()

                # save summary for tensorboard visualization
                log_running_loss += loss.item()
                log_running_err += error.item()
                
                if iteration % self.log_summary_step == (self.log_summary_step - 1):
                    self.build_train_summaries( 
                          left_rgb, 
                          None, #right_rgb,
                          # in the latest versions of PyTorch you can add a new axis by indexing with None 
                          # > see: https://discuss.pytorch.org/t/what-is-the-difference-between-view-and-unsqueeze/1155;
                          #torch.unsqueeze(disp0, dim=1) ==> disp0[:,None]
                          disp[:,None], target[:,None],
                          train_global_step, 
                          log_running_loss/self.log_summary_step, 
                          log_running_err/self.log_summary_step, 
                          is_KT15Color = False,
                          #is_KT15Color = True
                          imgl_aug = imgl_aug,
                          imgr_aug = None
                          )
                    # reset to zeros
                    log_running_loss = 0.0
                    log_running_err = 0.0

        
        # end of data_loader
        # save the checkpoints
        avg_loss = epoch_loss / valid_iteration
        avg_err = epoch_epe / valid_iteration
        avg_accu = epoch_accu3 / valid_iteration
        print("===> Epoch {} Complete: Avg. Loss: {:.4f}, Avg. EPE Error: {:.4f}, Accu{:.1f}: {:.4f})".format(
                  epoch, avg_loss, avg_err, accu_thred, avg_accu))

        is_best = False
        model_state_dict = {
                        'epoch': epoch,
                        'state_dict': self.model.state_dict(),
                        'optimizer' : self.optimizer.state_dict(),
                        'loss': avg_loss,
                        'epe_err': avg_err, 
                        'accu3': avg_accu
                    }

        if self.kitti2012 or self.kitti2015:
            #if epoch % 50 == 0 and epoch >= 300:
            #if epoch % 50 == 0:
            if epoch % 25 == 0:
                self.save_checkpoint(epoch, model_state_dict, is_best)
        else:
            #if epoch >= 7:
            #    self.save_checkpoint(epoch, model_state_dict, is_best)
            self.save_checkpoint(epoch, model_state_dict, is_best)
        # avg loss
        return avg_loss, avg_err, avg_accu


    #---------------------
    #---- Test ----- -----
    #---------------------
    def test(self):
        self.model.eval()
        file_path = self.args.data_path
        file_list_txt = self.args.test_list
        f = open(file_list_txt, 'r')
        if self.virtual_kitti2:
            filelist = get_virtual_kitti2_filelist(file_list_txt)
        else:
            filelist = [l.rstrip() for l in f.readlines() if not l.rstrip().startswith('#')]
        
        avg_err = 0
        avg_rate1 = 0
        avg_rate3 = 0
        
        crop_width = self.args.crop_width
        crop_height = self.args.crop_height
        
        if not os.path.exists(self.args.resultDir):
            os.makedirs(self.args.resultDir)
            print ('makedirs {}'.format(self.args.resultDir))
        
        img_num = len(filelist)
        print ("[***]To test %d imgs" %img_num)
        for index in range(len(filelist)):
            current_file = filelist[index]
            if self.kitti2015:
                data_type_str= "kt15"
                leftname = pjoin(file_path, 'image_0/' + current_file)
                #print ("limg: {}".format(leftname))
                if index < 1:
                    print ("limg: {}".format(leftname))
                rightname = pjoin(file_path, 'image_1/' + current_file)
                dispname = pjoin(file_path, 'disp_occ_0_pfm/' + current_file[0:-4] + '.pfm')
                if os.path.isfile(dispname):
                    dispGT = pfm.readPFM(dispname)
                    dispGT[dispGT == np.inf] = .0
                else:
                    dispGT= None
                savename = pjoin(self.args.resultDir, current_file[0:-4] + '.pfm')
                
            elif self.kitti2012:
                data_type_str= "kt12"
                #leftname = pjoin(file_path, 'image_0/' + current_file)
                leftname =  pjoin(file_path, 'colored_0/' + current_file)
                rightname = pjoin(file_path, 'colored_1/' + current_file)
                #print ("limg: {}".format(leftname))
                dispname = pjoin(file_path, 'disp_occ_0_pfm/' + current_file[0:-4] + '.pfm')
                if os.path.isfile(dispname):
                    dispGT = pfm.readPFM(dispname)
                    dispGT[dispGT == np.inf] = .0
                else:
                    dispGT= None
                savename = pjoin(self.args.resultDir, current_file[0:-4] + '.pfm')
                #disp = Image.open(dispname)
                #disp = np.asarray(disp) / 256.0

            elif self.virtual_kitti2:
                data_type_str= "virtual_kt2" 
                A = current_file 
                # e.g., /media/ccjData2/datasets/Virtual-KITTI-V2/vkitti_2.0.3_rgb/Scene01/15-deg-left/frames/rgb/Camera_0/rgb_00001.jpg
                leftname = pjoin(file_path, "vkitti_2.0.3_rgb/" + A) 
                rightname = pjoin(file_path, "vkitti_2.0.3_rgb/" + A[:-22] + 'Camera_1/' + A[-13:])
                #load depth GT and change it to disparity GT: 
                depth_png_filename = pjoin(file_path, "vkitti_2.0.3_depth/" + A[:-26] + 'depth/Camera_0/depth_' + A[-9:-4] + ".png")
                #print ("[???]imgl = ", leftname, ", imgr = ", rightname, ", depth_left = ", depth_png_filename)
                #NOTE: The depth map in centimeters can be directly loaded
                depth_left = cv2.imread(depth_png_filename, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
                height, width = depth_left.shape[:2]
                # Intrinsi: f_x = f_y = 725.0087 
                # offset(i.e., distance between stereo views): B = 0.532725 m = 53.2725 cm
                B = 53.2725 # in centimeters;
                f = 725.0087 # in pixels
                # set zero as a invalid disparity value;
                dispGT = np.zeros([height, width], 'float32')
                mask = depth_left > 0
                dispGT[mask] = f*B/ depth_left[mask] # d = fB/z
                #pfm.show(dispGT, title='dispGT')
                savename = pjoin(self.args.resultDir, '%04d.pfm'%(index))

            else:
                A = current_file
                leftname = pjoin(file_path, A)
                rightname = pjoin(file_path, A[:-13] + 'right/' + A[len(A)-8:]) 
                 # check disparity GT exists or not!!!
                pos = A.find('/')
                tmp_len = len('frames_finalpass')
                dispname = pjoin(file_path, A[0:pos] + '/disparity' + A[pos+1+tmp_len:-4] + '.pfm')
                #print ("[****] ldisp: {}".format(dispname))
                if os.path.isfile(dispname):
                    dispGT = pfm.readPFM(dispname)
                    dispGT[dispGT == np.inf] = .0
                else:
                    dispGT= None
                savename = pjoin(self.args.resultDir, str(index) + '.pfm')

            input1, input2, height, width = test_transform(
                    load_test_data(leftname, rightname, 
                                is_data_augment=self.is_data_augment), 
                    crop_height, crop_width)
            if self.cuda:
                input1 = input1.cuda()
                input2 = input2.cuda()
            with torch.no_grad():
                prediction = self.model(input1, input2)
            
            disp = prediction.cpu().detach().numpy()
            if height <= crop_height and width <= crop_width:
                disp = disp[0, crop_height - height: crop_height, crop_width-width: crop_width]
            else:
                disp = disp[0, :, :]
            #skimage.io.imsave(savename, (disp * 256).astype('uint16'))
            #pfm.save(savename, disp)
            #print ('savded ', savename)
            # save kt15 color
            
            #if self.kitti2015 or self.kitti2012:
            if any([self.kitti2015, self.kitti2012, index % 250 == 0]):
                """ disp """
                pfm.save(savename, disp)
                
                tmp_dir = pjoin(self.args.resultDir, "dispColor")
                if not os.path.exists(tmp_dir):
                    os.makedirs(tmp_dir)
                tmp_dispname = pjoin(tmp_dir, current_file[0:-4] + '.png')
                cv2.imwrite(tmp_dispname, 
                        KT15FalseClr.writeKT15FalseColor(np.ascontiguousarray(disp)).astype(np.uint8)[:,:,::-1])
                
                if index % 50 == 0:
                    print ('savded ', tmp_dispname)
                
                if dispGT is not None: #If KT benchmark submission, then No dispGT;
                    """ err-disp """
                    tmp_dir = pjoin(self.args.resultDir, "errDispColor")
                    if not os.path.exists(tmp_dir):
                        os.makedirs(tmp_dir)
                    tmp_errdispname = pjoin(tmp_dir, current_file[0:-4]  + '.png')
                    cv2.imwrite(tmp_errdispname, 
                            KT15LogClr.writeKT15ErrorDispLogColor(np.ascontiguousarray(disp), 
                                np.ascontiguousarray(dispGT)).astype(np.uint8)[:,:,::-1])
                    
                    if index % 50 == 0:
                        print ('savded ', tmp_errdispname)
            
            error, rate1, rate3 = get_epe_rate(dispGT, disp, threshold=1.0, threshold2=3.0)
            avg_err += error
            avg_rate1 += rate1
            avg_rate3 += rate3

            if index % 250 == 0:
                message_info = "===> Frame {}: ".format(index) + current_file + " ==> EPE Error: {:.4f}, Bad-{:.1f} Error: {:.4f}, Bad-{:.1f} Error: {:.4f}".format(
                    error, 1.0, rate1, 3.0, rate3)
                print (message_info)
                #sys.stdout.flush()
        
        # end of test data loop
        if dispGT is not None:
            avg_err /= img_num
            avg_rate1 /= img_num
            avg_rate3 /= img_num
            print("===> Total {} Frames ==> AVG EPE Error: {:.4f}, AVG Bad-{:.1f} Error: {:.4f}, AVG Bad-{:.1f} Error: {:.4f}".format(
                img_num, avg_err, 1.0, avg_rate1, 3.0, avg_rate3))
        
        """ save as csv file, Excel file format """
        csv_file = os.path.join(self.args.resultDir, 'bad-err.csv')
        print ("write ", csv_file, "\n")
        timeStamp = datetime.now().strftime('%Y-%m-%d_%H:%M:%S')
        messg = timeStamp + ',{},bad-1.0,{:.4f},bad-3.0,{:.4f},epe,{:.4f},fileDir={},for log,{:.3f}(epe); {:.3f}%(bad1); {:.3f}%(bad3)\n'.format(
            data_type_str, avg_rate1, avg_rate3, avg_err, 
            self.args.resultDir, 
            avg_err, avg_rate1*100.0, avg_rate3*100.0)
        
        with open( csv_file, 'w') as fwrite:
            fwrite.write(messg)
        
        print ('GCNet testing finished!')


def main(args):
    #----------------------------
    # some initilization thing 
    #---------------------------
    cuda = args.cuda
    if cuda and not torch.cuda.is_available():
        raise Exception("No GPU found, please run without --cuda")
    torch.manual_seed(args.seed)
    if cuda:
        torch.cuda.manual_seed(args.seed)
    
    myNet = MyGCNet(args)
    
    print('Number of GCNet model parameters: {}'.format(
            sum([p.data.nelement() for p in myNet.model.parameters()])))
    if 0: 
        print('Including:\n1) number of Feature Extraction module parameters: {}'.format(
            sum(
                [p.data.nelement() for n, p in myNet.model.named_parameters() if any(
                    ['module.convbn0' in n, 
                     'module.res_block' in n, 
                     'module.conv1' in n
                     ])]
                )))
        print('2) number of Other modules parameters: {}'.format(
            sum(
                [p.data.nelement() for n, p in myNet.model.named_parameters() if any(
                    ['module.conv3dbn' in n,
                     'module.block_3d' in n,
                     'module.deconv' in n,
                     ])]
                )))

        for i, (n, p) in enumerate(myNet.model.named_parameters()):
            print (i, "  layer ", n, "has # param : ", p.data.nelement())
        #sys.exit()

    """ for debugging """
    if args.mode == 'debug':
        myNet.model.train()
        import gc
        crop_h = 256
        crop_w = 512
        #x_l = torch.randn((1, 3, crop_h, crop_w), requires_grad=True)
        #x_r = torch.randn((1, 3, crop_h, crop_w), requires_grad=True)
        x_l = torch.randn((1, 3, crop_h, crop_w)).cuda()
        x_r = torch.randn((1, 3, crop_h, crop_w)).cuda()
        y = torch.randn((1, crop_h, crop_w)).cuda()
        z = torch.randn((1, 1, crop_h//3, crop_w//3)).cuda()

        
        from pytorch_memlab import profile, MemReporter
        # pass in a model to automatically infer the tensor names
        # You can better understand the memory layout for more complicated module
        if 1:
            reporter = MemReporter(myNet.model)
            disp = myNet.model(x_l, x_r)
            loss = F.smooth_l1_loss(disp, y, reduction='mean')
            reporter.report(verbose=True)
            print('========= before backward =========')
            loss.backward()
            reporter.report(verbose=True)

        # generate prof which can be loaded by Google chrome trace at chrome://tracing/
        if 1:
            with torch.autograd.profiler.profile(use_cuda=True) as prof:
                myNet.model(x_l, x_r)
            print(prof)
            prof.export_chrome_trace('./results/tmp/prof.out')
    
    if args.mode == 'train':
        print('strat training !!!')
        for epoch in range(1 + args.startEpoch, args.startEpoch + args.nEpochs + 1):
            print ("[**] do training at epoch %d/%d" % (epoch, args.startEpoch + args.nEpochs))

            with torch.autograd.set_detect_anomaly(True):
                avg_loss, avg_err, avg_accu = myNet.train(epoch)
        # save the last epoch always!!
        myNet.save_checkpoint(args.nEpochs + args.startEpoch,
            {
                'epoch': args.nEpochs + args.startEpoch,
                'state_dict': myNet.model.state_dict(),
                'optimizer' : myNet.optimizer.state_dict(),
                'loss': avg_loss,
                'epe_err': avg_err, 
                'accu3': avg_accu
            }, 
            is_best = False)
        print('done training !!!')
    
    if args.mode == 'test': 
        print('strat testing !!!')
        myNet.test()


if __name__ == '__main__':
    
    import argparse
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch GANet Example')
    parser.add_argument('--crop_height', type=int, required=True, help="crop height")
    parser.add_argument('--max_disp', type=int, default=192, help="max disp")
    parser.add_argument('--crop_width', type=int, required=True, help="crop width")
    parser.add_argument('--resume', type=str, default='', help="resume from saved model")
    parser.add_argument('--batchSize', type=int, default=1, help='training batch size')
    parser.add_argument('--log_summary_step', type=int, default=200, help='every 200 steps to build training summary')
    parser.add_argument('--nEpochs', type=int, default=400, help='number of epochs to train for')
    parser.add_argument('--startEpoch', type=int, default=0, help='starting point, used for fine-tuning')
    parser.add_argument('--lr', type=float, default=0.001, help='Learning Rate. Default=0.001')
    parser.add_argument('--cuda', type=int, default=1, help='use cuda? Default=True')
    parser.add_argument('--threads', type=int, default=1, help='number of threads for data loader to use')
    parser.add_argument('--seed',  type=int, default=123, help='random seed to use. Default=123')
    parser.add_argument('--shift', type=int, default=0, help='random shift of left image. Default=0')
    parser.add_argument('--kitti2012', type=int, default=0, help='kitti 2012 dataset? Default=False')
    parser.add_argument('--kitti2015', type=int, default=0, help='kitti 2015? Default=False')
    parser.add_argument('--virtual_kitti2', type=int, default=0, help='virtual_kitti2? Default=False')
    parser.add_argument('--data_path', type=str, default='/data/ccjData', help="data root")
    parser.add_argument('--training_list', type=str, default='./lists/sceneflow_train.list', help="training list")
    parser.add_argument('--test_list', type=str, default='./lists/sceneflow_test_select.list', help="evaluation list")
    parser.add_argument('--checkpoint_dir', type=str, default='./checkpoint/', help="location to save models")
    parser.add_argument('--train_logdir', dest='train_logdir',  default='./logs/tmp', help='log dir')
    """Arguments related to run mode"""
    parser.add_argument('--model_name', type=str, default='GCNet', help="model name")
    parser.add_argument('--mode', dest='mode', type = str, default='train', help='train, test')
    parser.add_argument('--resultDir', type=str, default= "./results")
    #parser.add_argument('--threshold', type=float, default=3.0, help="threshold of error rates")
    #newly added ???
    parser.add_argument('--is_quarter_size_cost_volume_gcnet', type=str, default= 'false', help='flag to generate quarter_size cost volume')
    parser.add_argument('--is_kendall_version', type=str, default= 'false', help="flag to use kendall's original version")
    # added by CCJ on 2020/05/27;
    parser.add_argument('--kt12_image_mode', type=str, default= "rgb", help='flag to load kt2012 gray images, rgb, or gray2rgb')
    parser.add_argument('--is_data_augment', type=str, default= "false", help='flag to use data_augment, including random scale crop, color change etc')
    args = parser.parse_args()
    print('[***] args = ', args)
    main(args)
