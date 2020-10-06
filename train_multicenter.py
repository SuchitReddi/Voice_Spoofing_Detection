import torch
import torch.nn as nn
import argparse
import os
import json
import shutil
import numpy as np
import model as model_
from resnet import ResNet
from dataset import ASVspoof2019
from torch.utils.data import DataLoader
from evaluate_tDCF_asvspoof19 import compute_eer_and_tdcf
from loss import CenterLoss, LGMLoss_v0, LMCL_loss, IsolateLoss, MultiCenterIsolateLoss
from collections import defaultdict
from tqdm import tqdm, trange
from sklearn.cluster import KMeans
from utils import *

torch.set_default_tensor_type(torch.FloatTensor)

def initParams():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('--seed', type=int, help="random number seed", default=999)

    # Data folder prepare
    parser.add_argument("-d", "--path_to_database", type=str, help="dataset path", default='/data/neil/DS_10283_3336/')
    parser.add_argument("-f", "--path_to_features", type=str, help="features path",
                        default='/dataNVME/neil/ASVspoof2019Features/')
    parser.add_argument("-p", "--path_to_protocol", type=str, help="protocol path",
                        default='/data/neil/DS_10283_3336/LA/ASVspoof2019_LA_cm_protocols/')
    parser.add_argument("-o", "--out_fold", type=str, help="output folder", required=True, default='./models/try/')

    # Dataset prepare
    parser.add_argument("--feat", type=str, help="which feature to use", required=True,
                        choices=["CQCC", "LFCC", "MFCC", "STFT", "Melspec", "CQT"], default='CQCC')
    parser.add_argument("--feat_len", type=int, help="features length", default=650)
    parser.add_argument('--pad_chop', type=bool, default=False, help="whether pad_chop in the dataset")
    parser.add_argument("--enc_dim", type=int, help="encoding dimension", default=16)

    parser.add_argument('-m', '--model', help='Model arch', required=True,
                        choices=['cnn', 'resnet', 'tdnn', 'lstm', 'rnn', 'cnn_lstm'], default='cnn')

    # Training hyperparameters
    parser.add_argument('--num_epochs', type=int, default=80, help="Number of epochs for training")
    parser.add_argument('--batch_size', type=int, default=32, help="Mini batch size for training")
    parser.add_argument('--lr', type=float, default=0.0003, help="learning rate")
    parser.add_argument('--beta_1', type=float, default=0.9, help="bata_1 for Adam")
    parser.add_argument('--beta_2', type=float, default=0.999, help="beta_2 for Adam")
    parser.add_argument('--eps', type=float, default=1e-8, help="epsilon for Adam")
    parser.add_argument("--gpu", type=str, help="GPU index", default="1")
    parser.add_argument('--num_workers', type=int, default=0, help="number of workers")

    parser.add_argument('--weight_loss', type=float, default=1, help="weight for other loss")
    parser.add_argument('--r_real', type=float, default=0.5, help="r_real for isolate loss")
    parser.add_argument('--r_fake', type=float, default=30, help="r_fake for isolate loss")
    parser.add_argument('--accumulate_step', type=int, default=1, help="num of steps to update model")

    parser.add_argument('--enable_tag', type=bool, default=False, help="use tags as multi-class label")
    parser.add_argument('--visualize', action='store_true', help="feature visualization")
    parser.add_argument('--test_only', action='store_true', help="test the trained model in case the test crash sometimes or another test method")

    parser.add_argument('--pre_train', action='store_true', help="whether to pretrain the model")
    parser.add_argument('--prtrn_mthd', type=str, default='cross_entropy',
                            choices=['cross_entropy', 'single_center'], help="pretrain method, in other words, pretrain with what loss")

    args = parser.parse_args()

    # Change this to specify GPU
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    # Set seeds
    os.environ['PYTHONHASHSEED'] = str(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    if args.test_only:
        pass
    else:
        # Path for output data
        if not os.path.exists(args.out_fold):
            os.makedirs(args.out_fold)
        else:
            shutil.rmtree(args.out_fold)
            os.mkdir(args.out_fold)

        # Folder for intermediate results
        if not os.path.exists(os.path.join(args.out_fold, 'checkpoint')):
            os.makedirs(os.path.join(args.out_fold, 'checkpoint'))
        else:
            shutil.rmtree(os.path.join(args.out_fold, 'checkpoint'))
            os.mkdir(os.path.join(args.out_fold, 'checkpoint'))

        # Path for input data
        assert os.path.exists(args.path_to_database)
        assert os.path.exists(args.path_to_features)

        # Save training arguments
        with open(os.path.join(args.out_fold, 'args.json'), 'w') as file:
            file.write(json.dumps(vars(args), sort_keys=True, separators=('\n', ':')))

    args.cuda = torch.cuda.is_available()
    print('Cuda device available: ', args.cuda)
    args.device = torch.device("cuda" if args.cuda else "cpu")
    if int(args.gpu) == 5:
        args.device = torch.device("cpu")

    with open(os.path.join(args.out_fold, 'train_loss.log'), 'w') as file:
        file.write("Start recording training loss ...")
    with open(os.path.join(args.out_fold, 'dev_loss.log'), 'w') as file:
        file.write("Start recording validation loss ...")

    return args

def pre_train(args, trainDataLoader, model, model_optimizer):
    trainlossDict = defaultdict(list)
    model.train()
    feat_loader, label_loader = [], []
    with trange(len(trainDataLoader)) as t:
        for i in t:
            cqcc, audio_fn, tags, labels = [d for d in next(iter(trainDataLoader))]
            cqcc = cqcc.unsqueeze(1).float().to(args.device)
            labels = labels.to(args.device)
            feats, cqcc_outputs = model(cqcc)

            if args.prtrn_mthd == "cross_entropy":
                criterion = nn.CrossEntropyLoss()
                cqcc_loss = criterion(cqcc_outputs, labels)
                model_optimizer.zero_grad()
                trainlossDict["feat_loss"].append(cqcc_loss.item())
                cqcc_loss.backward()
                model_optimizer.step()

            # if pretrain_method == "center":
            #     centerloss = centerLoss(feats, labels)
            #     trainlossDict["feat"].append(cqcc_loss.item())
            #     cqcc_loss += centerloss * args.weight_loss
            #     cqcc_optimizer.zero_grad()
            #     center_optimzer.zero_grad()
            #     trainlossDict["center"].append(centerloss.item())
            #     cqcc_loss.backward()
            #     cqcc_optimizer.step()
            #     # for param in centerLoss.parameters():
            #     #     param.grad.data *= (1. / args.weight_loss)
            #     center_optimzer.step()
            #
            # if pretrain_method == "isolate":
            #     isoloss = iso_loss(feats, labels)
            #     trainlossDict["feat"].append(cqcc_loss.item())
            #     cqcc_loss = isoloss * args.weight_loss
            #     cqcc_optimizer.zero_grad()
            #     iso_optimzer.zero_grad()
            #     trainlossDict["iso"].append(isoloss.item())
            #     cqcc_loss.backward()
            #     cqcc_optimizer.step()
            #     iso_optimzer.step()

            desc_str = ''
            for key in sorted(trainlossDict.keys()):
                desc_str += key + ':%.5f' % (np.nanmean(trainlossDict[key])) + ', '
            t.set_description(desc_str)

    model.eval()
    with torch.no_grad():
        for i, (feat, _, tags, labels) in enumerate(trainDataLoader):
            feat = feat.unsqueeze(1).float().to(args.device)
            new_feats, _ = model(feat)
            feat_loader.append(new_feats)
            label_loader.append(labels)
    feats = torch.cat(feat_loader, 0)
    labels = torch.cat(label_loader, 0)
    visualize(args, feats.data.cpu().numpy(), labels.data.cpu().numpy(), 0)
    model.train()

    return model, model_optimizer


def train(args):
    torch.set_default_tensor_type(torch.FloatTensor)

    # initialize model
    if args.model == 'cnn':
        # node_dict = {"CQCC": 128, "CQT": 256, "LFCC": 128, "MFCC": 256, "Melspec": 90}
        # cqcc_model = model_.CQCC_ConvNet(2, int(args.feat_len / 100 * node_dict[args.feat])).to(args.device)
        # node_dict = {"CQCC": 67840, "Melspec": 101760, "CQT": 156032, "STFT": 210304}
        # cqcc_model = model_.CQCC_ConvNet(2, node_dict[args.feat], subband_attention=False).to(args.device)
        node_dict = {"CQCC": 10, "Melspec": 15, "CQT": 156032, "STFT": 210304}
        cqcc_model = model_.CQCC_ConvNet(2, node_dict[args.feat], subband_attention=True).to(args.device)
    elif args.model == 'tdnn':
        node_dict = {"CQCC": 90, "CQT": 192, "LFCC": 60, "MFCC": 50}
        cqcc_model = model_.TDNN_classifier(node_dict[args.feat], 2).to(args.device)
    elif args.model == 'rnn':
        node_dict = {"CQCC": 90, "CQT": 192, "LFCC": 60, "MFCC": 50}
        cqcc_model = model_.RNN(node_dict[args.feat], 512, 2, 2).to(args.device)
    elif args.model == 'cnn_lstm':
        node_dict = {"CQCC": 90, "CQT": 192, "LFCC": 60, "MFCC": 50}
        cqcc_model = model_.CNN_LSTM(nclasses=2).to(args.device)
    elif args.model == 'resnet':
        node_dict = {"CQCC": 4, "LFCC": 3, "Melspec": 6, "CQT": 8, "STFT": 11, "MFCC": 50}
        cqcc_model = ResNet(node_dict[args.feat], args.enc_dim, resnet_type='18', nclasses=2).to(args.device)

    # Loss and optimizer
    criterion = nn.CrossEntropyLoss()
    cqcc_optimizer = torch.optim.Adam(cqcc_model.parameters(), lr=args.lr,
                                      betas=(args.beta_1, args.beta_2), eps=args.eps, weight_decay=0.0005)

    training_set = ASVspoof2019(args.path_to_database, args.path_to_features, args.path_to_protocol, 'train',
                                args.feat, feat_len=args.feat_len, pad_chop=args.pad_chop)
    genuine_trainset = ASVspoof2019(args.path_to_database, args.path_to_features, args.path_to_protocol, 'train',
                                args.feat, feat_len=args.feat_len, pad_chop=args.pad_chop, genuine_only=True)
    validation_set = ASVspoof2019(args.path_to_database, args.path_to_features, args.path_to_protocol, 'dev',
                                  args.feat, feat_len=args.feat_len, pad_chop=args.pad_chop)
    trainDataLoader = DataLoader(training_set, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers,
                                 collate_fn=training_set.collate_fn)
    genuine_trainDataLoader = DataLoader(genuine_trainset, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers,
                                 collate_fn=genuine_trainset.collate_fn)
    valDataLoader = DataLoader(validation_set, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers,
                               collate_fn=validation_set.collate_fn)

    feat, _, _, _ = training_set[23]
    print("Feature shape", feat.shape)

    # centers = torch.randn((3, args.enc_dim)) * 10
    # centers = centers.to(args.device)
    if args.pre_train:
        cqcc_model, cqcc_optimizer = pre_train(args, trainDataLoader, cqcc_model, cqcc_optimizer)
    centers = seek_centers_kmeans(args, 3, genuine_trainDataLoader, cqcc_model).to(args.device)
    multicenter_iso_loss = MultiCenterIsolateLoss(centers, 2, args.enc_dim, r_real=args.r_real,
                                                  r_fake=args.r_fake).to(args.device)

    early_stop_cnt = 0
    prev_loss = 1e8

    for epoch_num in tqdm(range(args.num_epochs)):
        ip1_loader, idx_loader = [], []
        cqcc_model.train()
        trainlossDict = defaultdict(list)
        devlossDict = defaultdict(list)
        print('\nEpoch: %d ' % (epoch_num + 1))
        cqcc_optimizer.zero_grad()
        # with trange(2) as t:
        with trange(len(trainDataLoader)) as t:
            for i in t:
                cqcc, audio_fn, tags, labels = [d for d in next(iter(trainDataLoader))]

                cqcc = cqcc.unsqueeze(1).float().to(args.device)

                labels = labels.to(args.device)
                feats, cqcc_outputs = cqcc_model(cqcc)
                cqcc_loss = criterion(cqcc_outputs, labels)

                trainlossDict["feat"].append(cqcc_loss.item())
                multiisoloss = multicenter_iso_loss(feats, labels)
                cqcc_loss = multiisoloss * args.weight_loss
                # cqcc_optimizer.zero_grad()
                trainlossDict["multiiso"].append(multiisoloss.item())
                cqcc_loss.backward()
                # cqcc_optimizer.step()

                if (i + 1) % args.accumulate_step == 0:
                    cqcc_optimizer.step()
                    cqcc_optimizer.zero_grad()
                    centers = seek_centers_kmeans(args, 3, genuine_trainDataLoader, cqcc_model).to(args.device)
                    multicenter_iso_loss = MultiCenterIsolateLoss(centers, 2, args.enc_dim, r_real=args.r_real,
                                                                  r_fake=args.r_fake).to(args.device)

                desc_str = ''
                for key in sorted(trainlossDict.keys()):
                    desc_str += key + ':%.5f' % (np.nanmean(trainlossDict[key])) + ', '
                t.set_description(desc_str)

                with open(os.path.join(args.out_fold, "train_loss.log"), "a") as log:
                    log.write(str(epoch_num) + "\t" + str(i) + "\t" + str(np.nanmean(trainlossDict[key])))

        # update center and loss_fn (before validation or after?)
        centers = seek_centers_kmeans(args, 3, genuine_trainDataLoader, cqcc_model).to(args.device)
        multicenter_iso_loss = MultiCenterIsolateLoss(centers, 2, args.enc_dim, r_real=args.r_real,
                                                      r_fake=args.r_fake).to(args.device)
        if args.visualize:
            with torch.no_grad():
                for i, (feat, _, tags, labels) in enumerate(trainDataLoader):
                    feat = feat.unsqueeze(1).float().to(args.device)
                    new_feats, _ = cqcc_model(feat)
                    ip1_loader.append(new_feats)
                    idx_loader.append(labels)
            feat = torch.cat(ip1_loader, 0)
            labels = torch.cat(idx_loader, 0)
            visualize(args, feat.data.cpu().numpy(), labels.data.cpu().numpy(), epoch_num+1)

        # Val the model
        # eval mode (batchnorm uses moving mean/variance instead of mini-batch mean/variance)
        cqcc_model.eval()

        with torch.no_grad():
            with trange(len(valDataLoader)) as v:
                for i in v:
                    cqcc, audio_fn, tags, labels = [d for d in next(iter(valDataLoader))]
                    if args.model == 'rnn':
                        cqcc = cqcc.transpose(1, 2).float().to(args.device)
                    else:
                        cqcc = cqcc.unsqueeze(1).float().to(args.device)
                    labels = labels.to(args.device)
                    feats, cqcc_outputs = cqcc_model(cqcc)
                    cqcc_loss = criterion(cqcc_outputs, labels)

                    multiisoloss = multicenter_iso_loss(feats, labels)
                    devlossDict["multiiso"].append(multiisoloss.item())

                    desc_str = ''
                    for key in sorted(devlossDict.keys()):
                        desc_str += key + ':%.5f' % (np.nanmean(devlossDict[key])) + ', '
                    v.set_description(desc_str)

                with open(os.path.join(args.out_fold, "dev_loss.log"), "a") as log:
                    log.write(str(epoch_num) + "\t" + str(np.nanmean(devlossDict[key])))

            valLoss = np.nanmean(devlossDict[key])

            # print("multi isolate centers: ", multicenter_iso_loss.centers.data)

            if valLoss < prev_loss:
                # Save the model checkpoint
                torch.save(cqcc_model, os.path.join(args.out_fold, 'anti-spoofing_cqcc_model.pt'))
                loss_model = multicenter_iso_loss
                torch.save(loss_model, os.path.join(args.out_fold, 'anti-spoofing_loss_model.pt'))

                prev_loss = valLoss
                early_stop_cnt = 0
            else:
                early_stop_cnt += 1

            if early_stop_cnt == 6:
                with open(os.path.join(args.out_fold, 'args.json'), 'a') as res_file:
                    res_file.write('\nTrained Epochs: %d\n' % (epoch_num - 5))
                break
            # if early_stop_cnt == 1:
            #     torch.save(cqcc_model, os.path.join(args.out_fold, 'anti-spoofing_cqcc_model.pt')

            # print('Dev Accuracy of the model on the val features: {} % '.format(100 * cqcc_correct / total))

    return cqcc_model, loss_model


def test(args, model, loss_model, part='eval'):
    test_set = ASVspoof2019(args.path_to_database, args.path_to_features, args.path_to_protocol, part,
                            args.feat, feat_len=args.feat_len, pad_chop=args.pad_chop)
    testDataLoader = DataLoader(test_set, batch_size=args.batch_size // 2, shuffle=False, num_workers=args.num_workers,
                                collate_fn=test_set.collate_fn)
    cqcc_correct = 0
    total = 0
    with open(os.path.join(args.out_fold, 'cm_score.txt'), 'w') as cm_score_file:
        for i, (cqcc, audio_fn, tags, labels) in enumerate(testDataLoader):
            if args.model == 'rnn':
                cqcc = cqcc.transpose(1, 2).float().to(args.device)
            else:
                cqcc = cqcc.unsqueeze(1).float().to(args.device)
            feats, cqcc_outputs = model(cqcc)
            labels = labels.to(args.device)

            _, cqcc_predicted = torch.max(cqcc_outputs.data, 1)
            total += labels.size(0)
            cqcc_correct += (cqcc_predicted == labels).sum().item()

            if (i + 1) % 20 == 0:
                print('Step [{}/{}] '.format(i + 1, len(testDataLoader)))
                print('Test Accuracy of the model on the eval features: {} %'.format(100 * cqcc_correct / total))
            for j in range(labels.size(0)):
                score = 1e8
                for k in range(loss_model.centers.shape[0]):
                    dist = torch.norm(feats[j] - loss_model.centers[k].unsqueeze(0), p=2, dim=1)
                    if dist.item() < score:
                        score = dist.item()
                cm_score_file.write(
                    '%s A%02d %s %s\n' % (audio_fn[j], tags[j].data, "spoof" if labels[j].data.cpu().numpy() else "bonafide", score))
    eer_cm, min_tDCF = compute_eer_and_tdcf(os.path.join(args.out_fold, 'cm_score.txt'), args)

    return eer_cm, min_tDCF


if __name__ == "__main__":
    args = initParams()
    if not args.test_only:
        _, _ = train(args)
    model = torch.load(os.path.join(args.out_fold, 'anti-spoofing_cqcc_model.pt'))
    # if args.test_only:
    loss_model = torch.load(os.path.join(args.out_fold, 'anti-spoofing_loss_model.pt'))
    TReer_cm, TRmin_tDCF = test(args, model, loss_model, "train")
    VAeer_cm, VAmin_tDCF = test(args, model, loss_model, "dev")
    TEeer_cm, TEmin_tDCF = test(args, model, loss_model)
    with open(os.path.join(args.out_fold, 'args.json'), 'a') as res_file:
        res_file.write('\nTrain EER: %8.5f min-tDCF: %8.5f\n' % (TReer_cm, TRmin_tDCF))
        res_file.write('\nVal EER: %8.5f min-tDCF: %8.5f\n' % (VAeer_cm, VAmin_tDCF))
        res_file.write('\nTest EER: %8.5f min-tDCF: %8.5f\n' % (TEeer_cm, TEmin_tDCF))