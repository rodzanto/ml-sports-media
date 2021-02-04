import argparse
import os
import json
import time
import random
import numpy as np
import mxnet as mx
from mxnet import autograd, gluon

import subprocess
subprocess.run(["pip",  "install", "gluoncv==0.9.2"])

import gluoncv as gcv
from gluoncv.data.batchify import Tuple, Stack, Pad
from gluoncv.data.transforms.presets.ssd import SSDDefaultTrainTransform
from gluoncv import model_zoo, data, utils

class GroundTruthDetectionDataset(gluon.data.Dataset):
    """
    Custom Dataset to handle the GroundTruthDetectionDataset
    """
    def __init__(self, label_path, data_path, task, split='train'):
        """
        Parameters
        ---------
        data_path: str, Path to the data folder, default 'data'
        split: str, Which dataset split to request, default 'train'
    
        """
        self.data_path = data_path
        self.image_info = []
        self.task = task
        with open(os.path.join(label_path,'output.manifest')) as f:
            lines = f.readlines()
            for line in lines:
                info = json.loads(line[:-1])
                if len(info[self.task]['annotations']):
                    self.image_info.append(info)
                    
        assert split in ['train', 'test', 'val']
        random.seed(1234)
        random.shuffle(self.image_info)
        l = len(self.image_info)
        
        if split == 'train':
            self.image_info = self.image_info[:int(0.85*l)]
        if split == 'val':
            self.image_info = self.image_info[int(0.85*l):int(l)]
        if split == 'test':
            self.image_info = self.image_info[int(0.99*l):]
        
    def __getitem__(self, idx):
        """
        Parameters
        ---------
        idx: int, index requested

        Returns
        -------
        image: nd.NDArray
            The image 
        label: np.NDArray bounding box labels of the form [[x1,y1, x2, y2, class], ...]
        """
        info = self.image_info[idx]
        image = mx.image.imread(os.path.join(self.data_path,info['source-ref'].split('/')[-1]))
        boxes = info[self.task]['annotations']
        label = []
        for box in boxes:
            label.append([box['left'], box['top'], box['left']+box['width'], box['top']+box['height'], box['class_id']])
     
        return image, np.array(label)
        
    def __len__(self):
        return len(self.image_info)

    def __len__(self):
        return len(self.image_info)

def get_dataloader(model, train_dataset, validation_dataset, height, width, batch_size, num_workers):
    """
    Get dataloader.
    """

    import gluoncv as gcv
    from gluoncv.data.batchify import Tuple, Stack, Pad
    from gluoncv.data.transforms.presets.ssd import SSDDefaultTrainTransform
    
    #In training mode, SSD returns three intermediate values
    #cls_preds are the class predictions prior to softmax
    #box_preds are bounding box offsets with one-to-one correspondence to anchors 
    with autograd.train_mode():
        _, _, anchors = model(mx.nd.zeros((1, 3, height, width)))
    batchify_fn = Tuple(Stack(), Stack(), Stack())  
    
    # SSDDefaultTrainTransform: data augmentation and prepprocessing
    # random color jittering, random expansion with prob 0.5, random cropping
    # resize with random interpolation, random horizontal flip, 
    # normalize (substract mean and divide by std)
    train_loader = gluon.data.DataLoader(
        train_dataset.transform(SSDDefaultTrainTransform(height, width, anchors)),
        batch_size, True, batchify_fn=batchify_fn, last_batch='rollover', num_workers=num_workers)
    
    return train_loader

def get_training_context(num_gpus):
    if num_gpus:
        return [mx.gpu(i) for i in range(num_gpus)]
    else:
        return mx.cpu()
        
def train(gt_labeling_task, epochs, base_network, classes, learning_rate, wd, momentum, model_dir, train, labels,
          current_host, hosts, num_gpus): 
    """
    Transfer learning.
    """
    import gluoncv as gcv
    from gluoncv import model_zoo, data, utils   

    # get the pretrained model and set classes to AWS
    model = gcv.model_zoo.get_model(base_network, classes=classes, pretrained_base=False, transfer='voc')
    
    #images and labels from Groundtruth are downloaded by Sagemaker into training instance
    train_dataset = GroundTruthDetectionDataset(split='train', 
                                                label_path=labels,
                                                data_path=train, 
                                                task=gt_labeling_task)
    val_dataset = GroundTruthDetectionDataset(split='val', 
                                              label_path=labels, 
                                              data_path=train, 
                                              task=gt_labeling_task)
    
    #define dataloader
    train_loader= get_dataloader(model, train_dataset, val_dataset, 512, 512, 16, 1)
    
    #check if GPUs are available
    ctx = get_training_context(num_gpus)
    
    #reassign parameters to context ctx
    model.collect_params().reset_ctx(ctx)
    
    #define Trainer 
    trainer = gluon.Trainer(model.collect_params(), 'sgd', {'learning_rate': learning_rate, 'wd': wd, 'momentum': momentum})

    # SSD losses: Confidence Loss (Cross entropy) + Location Loss (L2 loss)
    mbox_loss = gcv.loss.SSDMultiBoxLoss()
    ce_metric = mx.metric.Loss('CrossEntropy')
    smoothl1_metric = mx.metric.Loss('SmoothL1')

    # start transfer learning
    for epoch in range(0, epochs):
        
        ce_metric.reset()
        smoothl1_metric.reset()
        tic = time.time()
        btic = time.time()
        
        #hybridize model
        model.hybridize(static_alloc=True, static_shape=True)
        
        #iterate over training images
        for i, batch in enumerate(train_loader):
            
            #load data on the right context
            batch_size = batch[0].shape[0]
            
            #Splits an NDArray into len(ctx_list) slices and loads each slice to one context 
            data = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0)
            cls_targets = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0)
            box_targets = gluon.utils.split_and_load(batch[2], ctx_list=ctx, batch_axis=0)
            
            #forward pass
            with autograd.record():
                cls_preds = []
                box_preds = []
                for x in data:
                    cls_pred, box_pred, _ = model(x)
                    cls_preds.append(cls_pred)
                    box_preds.append(box_pred)
                sum_loss, cls_loss, box_loss = mbox_loss(
                    cls_preds, box_preds, cls_targets, box_targets)
                autograd.backward(sum_loss)
                
            #upate model parameters
            trainer.step(1)
            
            #update and print metrics
            ce_metric.update(0, [l * batch_size for l in cls_loss])
            smoothl1_metric.update(0, [l * batch_size for l in box_loss])
            name1, loss1 = ce_metric.get()
            name2, loss2 = smoothl1_metric.get()
            if i % 1 == 0:
                print('[Epoch {}][Batch {}], Speed: {:.3f} samples/sec, {}={:.3f}, {}={:.3f}'.format(
                    epoch, i, batch_size/(time.time()-btic), name1, loss1, name2, loss2))
            btic = time.time()
    
    #save model
    model.set_nms(nms_thresh=0.45, nms_topk=400, post_nms=100)
    model(mx.nd.ones((1,3,512,512), ctx=ctx[0]))
    model.export('%s/model' % model_dir)
    return model

def model_fn(model_dir):
    """
    Load the gluon model. Called once when hosting service starts.

    :param: model_dir The directory where model files are stored.
    :return: a model (in this case a Gluon network)
    """
    net = gluon.SymbolBlock.imports(
        '%s/model-symbol.json' % model_dir,
        ['data'],
        '%s/model-0000.params' % model_dir,
    )
   
    return net
    
def transform_fn(model, data, content_type, output_content_type): 
    """
    Transform incoming requests.
    """
    #decode json string into numpy array
    data = json.loads(data)
    
    #preprocess image   
    x, image = gcv.data.transforms.presets.ssd.transform_test(mx.nd.array(data), 512)
    
    #check if GPUs area available
    ctx = get_training_context(num_gpus)
    
    #load image onto right context
    x = x.as_in_context(ctx)
    
    #perform inference
    class_IDs, scores, bounding_boxes = model(x)
    
    #create list of results
    result = [class_IDs.asnumpy().tolist(), scores.asnumpy().tolist(), bounding_boxes.asnumpy().tolist()]
    
    #decode as json string
    response_body = json.dumps(result)
    return response_body, output_content_type

def neo_preprocess(payload, content_type):

    parsed = json.loads(payload)
    nda = np.array(parsed)

    return nda

def neo_postprocess(result):

    response_body = json.dumps(result)
    content_type = 'application/json'

    return response_body, content_type

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument('--gt_labeling_task', type=str, default='football2-60-od-images')
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--base_network', type=str, default='ssd_512_mobilenet1.0_custom')
    parser.add_argument('--classes', type=list, default=['ball', 'midfield', 'goal', 'cristiano'])
    parser.add_argument('--learning_rate', type=float, default=0.001)
    parser.add_argument('--wd', type=float, default=0.0005)
    parser.add_argument('--momentum', type=float, default=0.9)

    parser.add_argument('--model_dir', type=str, default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--train', type=str, default=os.environ['SM_CHANNEL_TRAIN'])
    parser.add_argument('--labels', type=str, default=os.environ['SM_CHANNEL_LABELS'])

    parser.add_argument('--current_host', type=str, default=os.environ['SM_CURRENT_HOST'])
    parser.add_argument('--hosts', type=list, default=json.loads(os.environ['SM_HOSTS']))

    return parser.parse_args()

if __name__ == '__main__':
    args = parse_args()
    num_gpus = int(os.environ['SM_NUM_GPUS'])
    train(args.gt_labeling_task, args.epochs, args.base_network, args.classes, args.learning_rate, args.wd, args.momentum,
          args.model_dir, args.train, args.labels, args.current_host, args.hosts, num_gpus)
