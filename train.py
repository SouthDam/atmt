import os
import logging
import argparse
import numpy as np
from tqdm import tqdm
from collections import OrderedDict

import torch
import torch.nn as nn

from seq2seq import models, utils
from seq2seq.data.dictionary import Dictionary
from seq2seq.data.dataset import Seq2SeqDataset, BatchSampler
from seq2seq.models import ARCH_MODEL_REGISTRY, ARCH_CONFIG_REGISTRY


def get_args():
    """ Defines training-specific hyper-parameters. """
    parser = argparse.ArgumentParser('Sequence to Sequence Model')
    parser.add_argument('--cuda', default=True, help='Use a GPU')

    # Add data arguments
    parser.add_argument('--data', default='baseline/prepared_data', help='path to data directory')
    parser.add_argument('--source-lang', default='de', help='source language')
    parser.add_argument('--target-lang', default='en', help='target language')
    parser.add_argument('--max-tokens', default=None, type=int, help='maximum number of tokens in a batch')
    parser.add_argument('--batch-size', default=100, type=int, help='maximum number of sentences in a batch')
    parser.add_argument('--train-on-tiny', action='store_true', help='train model on a tiny dataset')

    # Add model arguments
    parser.add_argument('--arch', default='lstm', choices=ARCH_MODEL_REGISTRY.keys(), help='model architecture')

    # Add optimization arguments
    parser.add_argument('--max-epoch', default=10000, type=int, help='force stop training at specified epoch')
    parser.add_argument('--clip-norm', default=4.0, type=float, help='clip threshold of gradients')
    parser.add_argument('--lr', default=0.0003, type=float, help='learning rate')
    parser.add_argument('--patience', default=5, type=int,
                        help='number of epochs without improvement on validation set before early stopping')

    # Add checkpoint arguments
    parser.add_argument('--log-file', default=None, help='path to save logs')
    parser.add_argument('--save-dir', default='checkpoints', help='path to save checkpoints')
    parser.add_argument('--restore-file', default='checkpoint_last.pt', help='filename to load checkpoint')
    parser.add_argument('--restore-file-rev', default='checkpoint_last_rev.pt', help='filename to load checkpoint')
    parser.add_argument('--save-interval', type=int, default=1, help='save a checkpoint every N epochs')
    parser.add_argument('--no-save', action='store_true', help='don\'t save models or checkpoints')
    parser.add_argument('--epoch-checkpoints', action='store_true', help='store all epoch checkpoints')

    # Parse twice as model arguments are not known the first time
    args, _ = parser.parse_known_args()
    model_parser = parser.add_argument_group(argument_default=argparse.SUPPRESS)
    ARCH_MODEL_REGISTRY[args.arch].add_args(model_parser)
    args = parser.parse_args()
    ARCH_CONFIG_REGISTRY[args.arch](args)
    return args

def get_diff(att, src_out, att_rev, src_out_rev):
    def calculate_diff(acontext, src_out_other):
        src_out_other = src_out_other.transpose(1, 2)
        diff = torch.bmm(acontext, src_out_other)
        diag = torch.diagonal(diff, dim1=1, dim2=2)
        numerator = torch.norm(diag, p=1)/len(diag)
        diff = diff.view(-1)
        denominator = (torch.norm(diff, p=1) - torch.norm(diag, p=1))/(len(diff) - len(diag))
        return numerator/denominator
    src_out = src_out.transpose(0, 1)
    src_out_rev = src_out_rev.transpose(0, 1)
    acontext = torch.bmm(att, src_out)
    acontext_rev = torch.bmm(att_rev, src_out_rev)
    
    d = calculate_diff(acontext, src_out_rev)
    d_rev = calculate_diff(acontext_rev, src_out)

    # print(d.cpu().detach().numpy())
    d=d.cpu().detach().numpy()
    d=torch.from_numpy(d)

    d_rev=d_rev.cpu().detach().numpy()
    d_rev=torch.from_numpy(d_rev)
    # print(src_out)
    # print(att2)
    return d, d_rev

def main(args):
    """ Main training function. Trains the translation model over the course of several epochs, including dynamic
    learning rate adjustment and gradient clipping. """

    logging.info('Commencing training!')
    torch.manual_seed(42)

    utils.init_logging(args)

    # Load dictionaries
    src_dict = Dictionary.load(os.path.join(args.data, 'dict.{:s}'.format(args.source_lang)))
    logging.info('Loaded a source dictionary ({:s}) with {:d} words'.format(args.source_lang, len(src_dict)))
    tgt_dict = Dictionary.load(os.path.join(args.data, 'dict.{:s}'.format(args.target_lang)))
    logging.info('Loaded a target dictionary ({:s}) with {:d} words'.format(args.target_lang, len(tgt_dict)))

    # Load datasets
    def load_data(split):
        return Seq2SeqDataset(
            src_file=os.path.join(args.data, '{:s}.{:s}'.format(split, args.source_lang)),
            tgt_file=os.path.join(args.data, '{:s}.{:s}'.format(split, args.target_lang)),
            src_dict=src_dict, tgt_dict=tgt_dict)

    train_dataset = load_data(split='train') if not args.train_on_tiny else load_data(split='tiny_train')
    valid_dataset = load_data(split='valid')

    # Build model and optimization criterion
    model = models.build_model(args, src_dict, tgt_dict)
    model_rev = models.build_model(args, tgt_dict, src_dict)
    logging.info('Built a model with {:d} parameters'.format(sum(p.numel() for p in model.parameters())))
    criterion = nn.CrossEntropyLoss(ignore_index=src_dict.pad_idx, reduction='sum')
    criterion2 = nn.MSELoss(reduction='sum')
    if args.cuda:
        model = model.cuda()
        model_rev = model_rev.cuda()
        criterion = criterion.cuda()

    # Instantiate optimizer and learning rate scheduler
    optimizer = torch.optim.Adam(model.parameters(), args.lr)

    # Load last checkpoint if one exists
    state_dict = utils.load_checkpoint(args, model, optimizer)  # lr_scheduler
    utils.load_checkpoint_rev(args, model_rev, optimizer)  # lr_scheduler
    last_epoch = state_dict['last_epoch'] if state_dict is not None else -1

    # Track validation performance for early stopping
    bad_epochs = 0
    best_validate = float('inf')

    for epoch in range(last_epoch + 1, args.max_epoch):
        train_loader = \
            torch.utils.data.DataLoader(train_dataset, num_workers=1, collate_fn=train_dataset.collater,
                                        batch_sampler=BatchSampler(train_dataset, args.max_tokens, args.batch_size, 1,
                                                                   0, shuffle=True, seed=42))
        model.train()
        model_rev.train()
        stats = OrderedDict()
        stats['loss'] = 0
        stats['lr'] = 0
        stats['num_tokens'] = 0
        stats['batch_size'] = 0
        stats['grad_norm'] = 0
        stats['clip'] = 0
        # Display progress
        progress_bar = tqdm(train_loader, desc='| Epoch {:03d}'.format(epoch), leave=False, disable=False)

        # Iterate over the training set
        for i, sample in enumerate(progress_bar):
            if args.cuda:
                sample = utils.move_to_cuda(sample)
            if len(sample) == 0:
                continue
            model.train()

            (output, att), src_out = model(sample['src_tokens'], sample['src_lengths'], sample['tgt_inputs'])
            # print(sample['src_lengths'])
            # print(sample['tgt_inputs'].size())
            # print(sample['src_tokens'].size())
            src_inputs = sample['src_tokens'].clone()
            src_inputs[0,1:src_inputs.size(1)] = sample['src_tokens'][0,0:(src_inputs.size(1)-1)]
            src_inputs[0,0] = sample['src_tokens'][0,src_inputs.size(1)-1]
            tgt_lengths = sample['src_lengths'].clone()#torch.tensor([sample['tgt_tokens'].size(1)])
            tgt_lengths += sample['tgt_inputs'].size(1) - sample['src_tokens'].size(1)
            # print(tgt_lengths)
            # print(sample['num_tokens'])
            
            # if args.cuda:
            #     tgt_lengths = tgt_lengths.cuda()
            (output_rev, att_rev), src_out_rev = model_rev(sample['tgt_tokens'], tgt_lengths, src_inputs)

            # notice that those are without masks already
            # print(sample['tgt_tokens'].view(-1))
            d, d_rev = get_diff(att, src_out, att_rev, src_out_rev)
            
            # print(sample['src_tokens'].size())
            # print(sample['tgt_inputs'].size())
            # print(att.size())
            # print(src_out.size())
            # print(acontext.size())
            # print(src_out_rev.size())
            # # print(sample['tgt_inputs'].dtype)
            # # print(sample['src_lengths'])
            # # print(sample['src_tokens'])
            # # print('output %s' % str(output.size()))
            # # print(att)
            # # print(len(sample['src_lengths']))
            # print(d)
            # print(d_rev)
            # print(criterion(output.view(-1, output.size(-1)), sample['tgt_tokens'].view(-1)) / len(sample['src_lengths']))
            # print(att2)
            # output=output.cpu().detach().numpy()
            # output=torch.from_numpy(output).cuda()
            # output_rev=output_rev.cpu().detach().numpy()
            # output_rev=torch.from_numpy(output_rev).cuda()
            loss = \
                criterion(output.view(-1, output.size(-1)), sample['tgt_tokens'].view(-1)) / len(sample['src_lengths'])  + d +\
                criterion(output_rev.view(-1, output_rev.size(-1)), sample['src_tokens'].view(-1)) / len(tgt_lengths) +d_rev
            loss.backward()
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_norm)
            # loss_rev = \
            #     criterion(output_rev.view(-1, output_rev.size(-1)), sample['src_tokens'].view(-1)) / len(tgt_lengths) 
            # loss_rev.backward()
            # grad_norm_rev = torch.nn.utils.clip_grad_norm_(model_rev.parameters(), args.clip_norm)
            optimizer.step()
            optimizer.zero_grad()

            # Update statistics for progress bar
            total_loss, num_tokens, batch_size = (loss-d-d_rev).item(), sample['num_tokens'], len(sample['src_tokens'])
            stats['loss'] += total_loss * len(sample['src_lengths']) / sample['num_tokens']
            # stats['loss_rev'] += loss_rev.item() * len(sample['src_lengths']) / sample['src_tokens'].size(0) / sample['src_tokens'].size(1)
            stats['lr'] += optimizer.param_groups[0]['lr']
            stats['num_tokens'] += num_tokens / len(sample['src_tokens'])
            stats['batch_size'] += batch_size
            stats['grad_norm'] += grad_norm
            stats['clip'] += 1 if grad_norm > args.clip_norm else 0
            progress_bar.set_postfix({key: '{:.4g}'.format(value / (i + 1)) for key, value in stats.items()},
                                     refresh=True)

        logging.info('Epoch {:03d}: {}'.format(epoch, ' | '.join(key + ' {:.4g}'.format(
            value / len(progress_bar)) for key, value in stats.items())))

        # Calculate validation loss
        valid_perplexity = validate(args, model, model_rev, criterion, valid_dataset, epoch)
        model.train()
        model_rev.train()

        # Save checkpoints
        if epoch % args.save_interval == 0:
            utils.save_checkpoint(args, model, model_rev, optimizer, epoch, valid_perplexity)  # lr_scheduler

        # Check whether to terminate training
        if valid_perplexity < best_validate:
            best_validate = valid_perplexity
            bad_epochs = 0
        else:
            bad_epochs += 1
        if bad_epochs >= args.patience:
            logging.info('No validation set improvements observed for {:d} epochs. Early stop!'.format(args.patience))
            break


def validate(args, model, model_rev, criterion, valid_dataset, epoch):
    """ Validates model performance on a held-out development set. """
    valid_loader = \
        torch.utils.data.DataLoader(valid_dataset, num_workers=1, collate_fn=valid_dataset.collater,
                                    batch_sampler=BatchSampler(valid_dataset, args.max_tokens, args.batch_size, 1, 0,
                                                               shuffle=False, seed=42))
    model.eval()
    model_rev.eval()
    stats = OrderedDict()
    stats['valid_loss'] = 0
    stats['num_tokens'] = 0
    stats['batch_size'] = 0

    # Iterate over the validation set
    for i, sample in enumerate(valid_loader):
        if args.cuda:
            sample = utils.move_to_cuda(sample)
        if len(sample) == 0:
            continue
        with torch.no_grad():
            # Compute loss
            (output, attn_scores), src_out = model(sample['src_tokens'], sample['src_lengths'], sample['tgt_inputs'])
            
            src_inputs = sample['src_tokens'].clone()
            src_inputs[0,1:src_inputs.size(1)] = sample['src_tokens'][0,0:(src_inputs.size(1)-1)]
            src_inputs[0,0] = sample['src_tokens'][0,src_inputs.size(1)-1]
            tgt_lengths = sample['src_lengths'].clone()#torch.tensor([sample['tgt_tokens'].size(1)])
            tgt_lengths += sample['tgt_inputs'].size(1) - sample['src_tokens'].size(1)
            (output_rev, attn_scores_rev), src_out_rev = model_rev(sample['tgt_tokens'], tgt_lengths, src_inputs)

            d, d_rev = get_diff(attn_scores, src_out, attn_scores_rev, src_out_rev)
            loss = criterion(output.view(-1, output.size(-1)), sample['tgt_tokens'].view(-1)) + d + \
                criterion(output_rev.view(-1, output_rev.size(-1)), sample['src_tokens'].view(-1)) / len(tgt_lengths) + d_rev
        # Update tracked statistics
        stats['valid_loss'] += loss.item()
        stats['num_tokens'] += sample['num_tokens']
        stats['batch_size'] += len(sample['src_tokens'])

    # Calculate validation perplexity
    stats['valid_loss'] = stats['valid_loss'] / stats['num_tokens']
    perplexity = np.exp(stats['valid_loss'])
    stats['num_tokens'] = stats['num_tokens'] / stats['batch_size']

    logging.info(
        'Epoch {:03d}: {}'.format(epoch, ' | '.join(key + ' {:.3g}'.format(value) for key, value in stats.items())) +
        ' | valid_perplexity {:.3g}'.format(perplexity))

    return perplexity


if __name__ == '__main__':
    # torch.backends.cudnn.enabled = False
    args = get_args()
    args.device_id = 0

    # Set up logging to file
    logging.basicConfig(filename=args.log_file, filemode='a', level=logging.INFO,
                        format='%(levelname)s: %(message)s')
    if args.log_file is not None:
        # Logging to console
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        logging.getLogger('').addHandler(console)

    main(args)
