# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import itertools
import numpy as np
import os

from fairseq.data import (
    ConcatDataset, Dictionary, ExtraLabelDataset, IndexedRawTextDataset,
    MonolingualDataset, TokenBlockDataset, TruncatedDictionary,
    IndexedDataset)

from . import FairseqTask, register_task


@register_task('language_modeling')
class LanguageModelingTask(FairseqTask):
    """
    Train a language model.

    Args:
        dictionary (Dictionary): the dictionary for the  input of the language model

        output_dictionary (Dictionary): the dictionary for the output of the language model.
        In most cases it will be the same as dictionary, but could possibly be a more limited
        version of the dictionary (if --output-dictionary-size is used).

        targets (List[str]): list of the target types that the language model should predict.
        Can be one of "self", "future", and "past". Defaults to "future".

    .. note::

        The language modeling task is compatible with :mod:`train.py <train>`,
        :mod:`generate.py <generate>`, :mod:`interactive.py <interactive>` and
        :mod:`eval_lm.py <eval_lm>`.

    The language modeling task provides the following additional command-line
    arguments:

    .. argparse::
        :ref: fairseq.tasks.language_modeling_parser
        :prog:
    """

    @staticmethod
    def add_args(parser):
        """Add task-specific arguments to the parser."""
        parser.add_argument('data', help='path to data directory')
        parser.add_argument('--sample-break-mode',
                            choices=['none', 'complete', 'eos'],
                            help='If omitted or "none", fills each sample with tokens-per-sample '
                                 'tokens. If set to "complete", splits samples only at the end '
                                 'of sentence, but may include multiple sentences per sample. '
                                 'If set to "eos", includes only one sentence per sample.')
        parser.add_argument('--tokens-per-sample', default=1024, type=int,
                            help='max number of tokens per sample for LM dataset')
        parser.add_argument('--raw-text', default=False, action='store_true',
                            help='load raw text dataset')
        parser.add_argument('--output-dictionary-size', default=-1, type=int,
                            help='limit the size of output dictionary')
        parser.add_argument('--self-target', action='store_true',
                            help='include self target')
        parser.add_argument('--future-target', action='store_true',
                            help='include future target')
        parser.add_argument('--past-target', action='store_true',
                            help='include past target')
        parser.add_argument('--ignore-targets', default='',
                            help='space separated target tokens to ignore when computing loss')
        parser.add_argument('--use-bos', default=False, action='store_true',
                            help='if true, uses a separate bos tokens to indicate beginning of string')
        parser.add_argument('--next-sentence-prediction', default=False, action='store_true',
                            help='if true, tries to load a lbl file containing next sentence prediction labels')

    def __init__(self, args, dictionary, output_dictionary, targets=None):
        super().__init__(args)
        self.dictionary = dictionary
        self.output_dictionary = output_dictionary

        if targets is None:
            targets = ['future']
        self.targets = targets

    @classmethod
    def setup_task(cls, args, **kwargs):
        """Setup the task (e.g., load dictionaries).

        Args:
            args (argparse.Namespace): parsed command-line arguments
        """
        dictionary = Dictionary.load(os.path.join(args.data, 'dict.txt'))
        if not args.ignore_targets:
            dictionary.memory_optimize()
        print('| dictionary: {} types'.format(len(dictionary)))
        output_dictionary = dictionary
        if hasattr(args, 'output_dictionary_size') and args.output_dictionary_size >= 0:
            output_dictionary = TruncatedDictionary(dictionary, args.output_dictionary_size)

        # upgrade old checkpoints
        if hasattr(args, 'exclude_self_target'):
            args.self_target = not args.exclude_self_target

        targets = []
        if getattr(args, 'self_target', False):
            targets.append('self')
        if getattr(args, 'future_target', False):
            targets.append('future')
        if getattr(args, 'past_target', False):
            targets.append('past')
        if len(targets) == 0:
            # standard language modeling
            targets = ['future']

        return cls(args, dictionary, output_dictionary, targets=targets)

    def build_model(self, args):
        model = super().build_model(args)

        for target in self.targets:
            if target not in model.supported_targets:
                raise ValueError('Unsupported language modeling target: {}'.format(target))

        return model

    def load_dataset(self, split, combine=False):
        """Load a given dataset split.

        Args:
            split (str): name of the split (e.g., train, valid, test)
        """

        loaded_datasets = []
        extra_labels = []

        for k in itertools.count():
            split_k = split + (str(k) if k > 0 else '')
            path = os.path.join(self.args.data, split_k)

            if self.args.raw_text and IndexedRawTextDataset.exists(path):
                ds = IndexedRawTextDataset(path, self.dictionary)
            elif not self.args.raw_text and IndexedDataset.exists(path):
                ds = IndexedDataset(path, fix_lua_indexing=True)
            else:
                if k > 0:
                    break
                else:
                    raise FileNotFoundError('Dataset not found: {} ({})'.format(split, self.args.data))

            loaded_datasets.append(
                TokenBlockDataset(
                    ds, self.args.tokens_per_sample, pad=self.dictionary.pad(), eos=self.dictionary.eos(),
                    break_mode=self.args.sample_break_mode, include_targets=True, use_bos=self.args.use_bos,
                    bos=self.dictionary.bos()
                ))
            if self.args.next_sentence_prediction:
                with open(path + '.lbl', 'r') as lbl_f:
                    lines = lbl_f.readlines()
                    extra_labels.extend(int(l) for l in lines)

            print('| {} {} {} examples'.format(self.args.data, split_k, len(loaded_datasets[-1])))

            if not combine:
                break

        if len(loaded_datasets) == 1:
            dataset = loaded_datasets[0]
            sizes = dataset.sizes
        else:
            dataset = ConcatDataset(loaded_datasets)
            sizes = np.concatenate([ds.sizes for ds in loaded_datasets])

        add_eos_for_other_targets = self.args.sample_break_mode is not None and self.args.sample_break_mode != 'none'

        self.datasets[split] = MonolingualDataset(
            dataset, sizes, self.dictionary, self.output_dictionary,
            add_eos_for_other_targets=add_eos_for_other_targets, shuffle=True,
            targets=self.targets, ignore_targets=list(filter(None, self.args.ignore_targets.split(' '))),
        )

        if self.args.next_sentence_prediction:
            self.datasets[split] = ExtraLabelDataset(self.datasets[split], extra_labels)

    @property
    def target_dictionary(self):
        """Return the :class:`~fairseq.data.Dictionary` for the language
        model."""
        return self.output_dictionary
