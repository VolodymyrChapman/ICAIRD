import torch
import matplotlib.pyplot as plt
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import pandas as pd
import os
import io
from torch.utils.data import Dataset
from PIL import Image, ImageOps
import torchvision.models as models
from O365 import Account
import argparse
import time
import atexit
from scipy.ndimage.filters import gaussian_filter

torch.set_printoptions(precision=4, linewidth=300)
np.set_printoptions(precision=4, linewidth=300)

################################################ SET UP SEED AND ARGS AND EXIT HANDLER

seed = 99


def set_seed(seed):
    torch.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)


parser = argparse.ArgumentParser()
parser.add_argument('--batch_size', type=int, default=1)
parser.add_argument('--epochs', type=int, default=100)
parser.add_argument('--lr', type=float, default=0.001)
parser.add_argument('--conv_thresh', type=float, default=0.01)
parser.add_argument('--save_freq', default=100, type=int)
parser.add_argument('--subset', default=9999999, type=int)
parser.add_argument('--train', default=False, action='store_true')
parser.add_argument('--load', default=False, action='store_true')
parser.add_argument('--path', default='./lc_mask.pth')
parser.add_argument('--note', default='')
parser.add_argument('--bin_occ', type=int, default=0)
parser.add_argument('--preview', default=False, action='store_true')

args = parser.parse_args()

time_taken = 0
epoch_loss = 0
last_batch_loss = 0
epoch = 0


def save_results():
    results = {'time': time_taken, 'last_batch_loss': last_batch_loss, 'epoch_loss': epoch_loss, 'epoch': epoch}
    results = {**results, **vars(args)}
    print(results)
    results = pd.DataFrame(results, index=[0])
    results.to_csv('results.csv', index=False, mode='a+', header=True)


atexit.register(save_results)

################################################ SET UP ONEDRIVE ACCESS


client_secret = 'uzTrTxL5HxBkD=n]PkBg9SQf4N?Lmn5='

client_id = '291b2960-9a18-4859-8315-6b099b9ee87a'

scopes = ['basic', 'onedrive_all']

credentials = (client_id, client_secret)

account = Account(credentials)


def authenticate():
    print('Authenticating...')
    if not account.is_authenticated:
        account.authenticate(scopes=scopes)
    print('Authenticated...')


authenticate()

storage = account.storage()

my_drive = storage.get_default_drive()
root_folder = my_drive.get_root_folder()

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print('Using device: ', device)


################################################ IMG HELPERS


def show_im(im):
    d = im.shape[-1]
    fig, ax = plt.subplots()
    im = im.reshape(-1, d)
    plt.imshow(im, cmap='gray')
    plt.show()
    plt.close(fig)


def save_im(im, name='image'):
    d = im.shape[-1]
    fig, ax = plt.subplots()
    im = im.reshape(-1, d)
    plt.imshow(im, cmap='gray')
    plt.savefig('lc_imgs/' + name + '.png')
    plt.close(fig)


def standardise(img):
    img = img - np.mean(img)
    img = img / (np.std(img) + 0.00000001)
    return img


def normalise(img):
    img = (img - np.min(img)) / max(np.max(img) - np.min(img), 0.0001)

    return img

def whiten(img):
    img = img - img.mean()
    cov = np.dot(img.T, img)
    d, vec = np.linalg.eigh(cov)
    diag = np.diag(1 / np.sqrt(d+0.0001))
    w = np.dot(np.dot(vec, diag), vec.T)

    return np.dot(img, w)


################################################ SETTING UP DATASET


if not os.path.exists('od_sample_paths.csv'):
    print('Building data paths...')
    data = my_drive.get_item_by_path('/lc_exported_tiles/Preliminary immune class').get_items()
    samples = pd.DataFrame(columns=['ID', 'Img', 'Mask'])
    keys = pd.DataFrame(columns=['ID', 'Stroma', 'Immune cells', 'Tumor'])
    for d in data:
        print(d.name)
        row = {}
        k_id = d.name.split('_')[0]
        if ').png' in d.name:
            row['ID'] = k_id
            row['Img'] = d.name
            row['Mask'] = os.path.splitext(d.name)[0] + '_mask.png'
            samples = samples.append(row, ignore_index=True)
        if 'key' in d.name:
            print('KF', end=' ')
            d.download('', 'key.txt')
            with open('key.txt', 'r') as key:
                key_row = {}
                key_row['ID'] = k_id
                key = key.read()
                lines = key.split('\n')
                for l in lines:
                    sp = l.split('\t')
                    if len(sp) == 2:
                        key_row[sp[0]] = int(sp[1])
                keys = keys.append(key_row, ignore_index=True)

    keys.to_csv('sample_keys.csv')
    samples = pd.merge(samples, keys, on='ID')
    samples.to_csv('od_sample_paths.csv')
    print('\n',len(samples))
    print('DONE!')


def prep1(img):
    img = normalise(img)
    mask = img.copy()
    mask[mask > 0.2] = 0
    mask[mask > 0] = 1
    mask = gaussian_filter(mask, sigma=0.2)
    mask[mask < 1] = 0.1
    mask = gaussian_filter(mask, sigma=1)
    img *= mask
    return


def prep(img):
    img = normalise(img)
    mask = img.copy()
    mask[mask > 0.2] = 0
    mask[mask > 0] = 1
    mask = gaussian_filter(mask, sigma=0.2)
    mask[mask < 1] = 0
    del_mask = mask.copy()
    mask = gaussian_filter(mask, sigma=1)
    img *= mask

    return img, del_mask


class lc_seg_tiles(Dataset):

    def __init__(self, subset=-1):
        self.cell_type = 'Immune cells'
        self.samples = pd.read_csv('od_sample_paths.csv')[:subset]


    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        s = self.samples.iloc[index]
        dim = 256
        name = s['Img']
        name = name.split('_')[0] + '_' + name.split(',')[1] + '_' + name.split(',')[2]
        img = my_drive.get_item_by_path('/lc_exported_tiles/exported_tiles/' + s['Img'])
        mask = my_drive.get_item_by_path('/lc_exported_tiles/exported_tiles/' + s['Mask'])

        img_buffer = io.BytesIO()
        mask_buffer = io.BytesIO()

        img = img.download(output=img_buffer)
        mask = mask.download(output=mask_buffer)

        img = np.array(Image.open(img_buffer))[:dim, :dim]

        img, del_mask = prep(img)

        img = np.pad(img, ((0, dim - img.shape[0]), (0, dim - img.shape[1])), 'minimum')

        mask = np.array(Image.open(mask_buffer))[:dim, :dim]

        mask = np.pad(mask, ((0, dim - mask.shape[0]), (0, dim - mask.shape[1])), 'minimum')

        img_buffer.close()
        mask_buffer.close()

        mask_none = mask.copy()
        mask_none[mask_none > 0] = 1
        img*= mask_none
        cell_key = s[self.cell_type]
        mask[mask != cell_key] = 0
        mask[mask == cell_key] = 1
        mask *= del_mask.astype('uint8')

        count = np.sum(mask)

        return np.expand_dims(img.astype(np.float32), 0), np.expand_dims(count.astype(np.float32), 0), np.expand_dims(mask.astype(np.float32), 0), name


################################################ MODELS


class SimpleConv(nn.Module):

    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, 10)

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2(x), 2))

        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x



""" Parts of the U-Net model 
https://github.com/milesial/Pytorch-UNet
"""


class DoubleConv(nn.Module):
    """(convolution => [BN] => ReLU) * 2"""

    def __init__(self, in_channels, out_channels, mid_channels=None):
        super().__init__()
        if not mid_channels:
            mid_channels = out_channels
        self.double_conv = nn.Sequential(
                nn.Conv2d(in_channels, mid_channels, kernel_size=3, padding=1),
                nn.BatchNorm2d(mid_channels),
                nn.ReLU(inplace=True),
                nn.Conv2d(mid_channels, out_channels, kernel_size=3, padding=1),
                nn.BatchNorm2d(out_channels),
                nn.ReLU(inplace=True)
                )

    def forward(self, x):
        return self.double_conv(x)


class Down(nn.Module):
    """Downscaling with maxpool then double conv"""

    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.maxpool_conv = nn.Sequential(
                nn.MaxPool2d(2),
                DoubleConv(in_channels, out_channels)
                )

    def forward(self, x):
        return self.maxpool_conv(x)


class Up(nn.Module):
    """Upscaling then double conv"""

    def __init__(self, in_channels, out_channels, bilinear=True):
        super().__init__()

        # if bilinear, use the normal convolutions to reduce the number of channels
        if bilinear:
            self.up = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
            self.conv = DoubleConv(in_channels, out_channels, in_channels // 2)
        else:
            self.up = nn.ConvTranspose2d(in_channels, in_channels // 2, kernel_size=2, stride=2)
            self.conv = DoubleConv(in_channels, out_channels)

    def forward(self, x1, x2):
        x1 = self.up(x1)
        # input is CHW
        diffY = x2.size()[2] - x1.size()[2]
        diffX = x2.size()[3] - x1.size()[3]

        x1 = F.pad(x1, [diffX // 2, diffX - diffX // 2,
                        diffY // 2, diffY - diffY // 2])
        # if you have padding issues, see
        # https://github.com/HaiyongJiang/U-Net-Pytorch-Unstructured-Buggy/commit/0e854509c2cea854e247a9c615f175f76fbb2e3a
        # https://github.com/xiaopeng-liao/Pytorch-UNet/commit/8ebac70e633bac59fc22bb5195e513d5832fb3bd
        x = torch.cat([x2, x1], dim=1)
        return self.conv(x)


class OutConv(nn.Module):

    def __init__(self, in_channels, out_channels):
        super(OutConv, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=1)

    def forward(self, x):
        return self.conv(x)


""" Full assembly of the parts to form the complete network """


class UNet(nn.Module):

    def __init__(self, n_channels, n_classes, bilinear=True):
        super(UNet, self).__init__()
        self.n_channels = n_channels
        self.n_classes = n_classes
        self.bilinear = bilinear

        self.inc = DoubleConv(n_channels, 64)
        self.down1 = Down(64, 128)
        self.down2 = Down(128, 256)
        self.down3 = Down(256, 512)
        factor = 2 if bilinear else 1
        self.down4 = Down(512, 1024 // factor)
        self.up1 = Up(1024, 512 // factor, bilinear)
        self.up2 = Up(512, 256 // factor, bilinear)
        self.up3 = Up(256, 128 // factor, bilinear)
        self.up4 = Up(128, 64, bilinear)
        self.outc = OutConv(64, n_classes)

    def forward(self, x):
        x1 = self.inc(x)
        x2 = self.down1(x1)
        x3 = self.down2(x2)
        x4 = self.down3(x3)
        x5 = self.down4(x4)
        x = self.up1(x5, x4)
        x = self.up2(x, x3)
        x = self.up3(x, x2)
        x = self.up4(x, x1)
        logits = self.outc(x)
        return logits


class ResNet(nn.Module):

    def __init__(self, num_outputs=1):
        super().__init__()
        self.rn = models.resnet50(num_classes=num_outputs)
        self.conv1 = nn.Conv2d(1, 3, 1)

    def forward(self, x):
        x = self.conv1(x)
        x = self.rn(x)
        return x


class fcn_resnet101(nn.Module):
    def __init__(self):
        super().__init__()
        self.fcn = models.segmentation.fcn_resnet101(pretrained=False, progress=True, num_classes=1, aux_loss=None)
        self.conv1 = nn.Conv2d(1,3,1)

    def forward(self, x):
        x = self.conv1(x)
        x = self.fcn(x)
        return x


class SimpleFConv(nn.Module):

    def __init__(self, num_outputs=1):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 24, kernel_size=5, padding=2)
        self.conv2 = nn.Conv2d(24, 48, kernel_size=5, padding=2)
        self.conv3 = nn.Conv2d(48, 96, kernel_size=5, padding=2)
        self.conv4 = nn.Conv2d(96, 96, kernel_size=5, padding=2)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = F.relu(self.conv4(x))
        x = torch.mean(x, dim=1, keepdim=True)
        return x

################################################ TRAINING


def run_epochs(net, dataloader, criterion, optimizer, num_epochs, path, save_freq=100, train=True):
    global time_taken
    global epoch_loss
    global epoch
    global last_batch_loss
    start_time = time.time()
    if not train:
        net.eval()

    for epoch in range(num_epochs):
        sum_epoch_loss = 0.0
        running_loss = 0.0
        for i, data in enumerate(dataloader):
            inputs, labels, masks, name = data

            inputs = inputs.to(device)
            # labels = labels.to(device)
            masks = masks.to(device)

            targets = masks

            outputs = net(inputs)['out']

            loss = criterion(outputs, targets)

            print('Epoch: {} Batch: {}/{} Batch Loss {}'.format(epoch, i, len(dataloader), loss.item()))

            if True in torch.isnan(loss):
                print('NaaaaaaaaaaN!')
                return net, epoch_loss
            if train:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            time_taken = np.round(time.time() - start_time)

            running_loss += loss.item()
            sum_epoch_loss += loss.item()
            epoch_loss = sum_epoch_loss / (i+1)

            mode = 'training' if train else 'validating'

            if args.preview:
                im = inputs[0].detach().cpu().numpy()
                o = outputs[0].detach().cpu().numpy()
                m = masks[0].detach().cpu().numpy()
                save_im(im, 'input')
                save_im(m, 'mask')
                save_im(o, 'output')


            if (i > 0) and (i % save_freq == 0):
                print('\n({}) Epoch: {} Batch: {}/{} Avg Loss over last {} batches: {}\n'.format(mode, epoch, i, len(dataloader), save_freq, running_loss / save_freq))
                running_loss = 0.0
                print('Saving images...')
                im = inputs[0].detach().cpu().numpy()
                o = outputs[0].detach().cpu().numpy()
                m = masks[0].detach().cpu().numpy()
                save_im(im, '{}_i'.format(name[0]))
                save_im(m, '{}_m'.format(name[0]))
                save_im(o, '{}_o_{}_{}'.format(name[0], epoch, i))
                if train:
                    torch.save(net.state_dict(), path)


        print('Completed epoch {}. Avg epoch loss: {}'.format(epoch, sum_epoch_loss/(i+1)))
        if train:
            torch.save(net.state_dict(), args.path)

        if epoch_loss < args.conv_thresh:
            return net

    print('Finished Training')
    return net


dataset = lc_seg_tiles(args.subset)

net = fcn_resnet101()
set_seed(seed)

num_params = sum(p.numel() for p in net.parameters() if p.requires_grad)
print("Model has {} parameters".format(num_params))

if torch.cuda.device_count() > 1:
    print("Using", torch.cuda.device_count(), "GPUs.")
    net = nn.DataParallel(net)

net.to(device)
num_epochs = args.epochs
criterion = nn.BCEWithLogitsLoss()
optimizer = optim.Adam(net.parameters(), lr=args.lr)

train_size = int(0.7 * len(dataset))
val_test_size = len(dataset) - train_size
val_size = int(0.5 * val_test_size)
test_size = val_test_size - val_size

train_data, val_test_data = torch.utils.data.random_split(dataset, [train_size, val_test_size])
val_data, test_data = torch.utils.data.random_split(val_test_data, [val_size, test_size])

train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, shuffle=True)
eval_loader = torch.utils.data.DataLoader(val_data, batch_size=args.batch_size, shuffle=True)

if args.load:
    net.load_state_dict(torch.load(args.path, map_location=torch.device(device)))

if args.train:
    print('Training network...')
    net = run_epochs(net, train_loader, criterion, optimizer, num_epochs, args.path, save_freq=min(train_size, args.save_freq))


################################################ VALIDATION

else:
    print('Evaluating network...')
    net.load_state_dict(torch.load(args.path))
    _ = run_epochs(net, eval_loader, criterion, None, 1, None, train=False, save_freq=1)
