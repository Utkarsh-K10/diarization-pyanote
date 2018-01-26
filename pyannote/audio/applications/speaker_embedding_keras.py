#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2017 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr

"""
Speaker embedding

Usage:
  pyannote-speaker-embedding data [--database=<db.yml> --duration=<duration> --step=<step> --heterogeneous] <root_dir> <database.task.protocol>
  pyannote-speaker-embedding train [--subset=<subset> --start=<epoch> --end=<epoch>] <experiment_dir> <database.task.protocol>
  pyannote-speaker-embedding validate [--subset=<subset> --from=<epoch> --to=<epoch> --every=<epoch>] <train_dir> <database.task.protocol>
  pyannote-speaker-embedding apply [--database=<db.yml> --step=<step> --internal] <validate.txt> <database.task.protocol> <output_dir>
  pyannote-speaker-embedding compare (<validate.txt> <legend>)... <output.png>
  pyannote-speaker-embedding -h | --help
  pyannote-speaker-embedding --version

Options:
  <root_dir>                 Set root directory. This script expects a
                             configuration file called "config.yml" to live in
                             this directory. See '"data" mode' section below
                             for more details.
  <database.task.protocol>   Set evaluation protocol (e.g. "Etape.SpeakerDiarization.TV")
  --database=<db.yml>        Path to database configuration file.
                             [default: ~/.pyannote/db.yml]
  --duration=<duration>      Set duration of embedded sequences [default: 3.2]
  --step=<step>              Set step between sequences, in seconds.
                             Defaults to 0.5 x <duration>.
  --heterogeneous            Allow heterogeneous sequences. In this case, the
                             label given to heterogeneous sequences is the most
                             overlapping one.
  --start=<epoch>            Restart training after that many epochs.
  --end=<epoch>              Stop training after than many epochs [default: 1000]
  <experiment_dir>           Set experiment directory. This script expects a
                             configuration file called "config.yml" to live
                             in this directory. See '"train" mode' section
                             for more details.
  --subset=<subset>          Set subset (train|developement|test).
                             In "train" mode, defaults subset is "train".
                             In "validate" mode, defaults to "development".
  --every=<epoch>            Defaults to every epoch [default: 1].
  --from=<epoch>             Start at this epoch [default: 0].
  <train_dir>                Path to directory created by "train" mode.
  --internal                 Extract internal representation.
  -h --help                  Show this screen.
  --version                  Show version.


Database configuration file:
    The database configuration provides details as to where actual files are
    stored. See `pyannote.audio.util.FileFinder` docstring for more information
    on the expected format.

"data" mode:

    A file called <root_dir>/config.yml should exist, that describes the
    feature extraction process (e.g. MFCCs):

    ................... <root_dir>/config.yml .........................
    feature_extraction:
       name: YaafeMFCC
       params:
          e: False                   # this experiments relies
          De: True                   # on 11 MFCC coefficients
          DDe: True                  # with 1st and 2nd derivatives
          D: True                    # without energy, but with
          DD: True                   # energy derivatives
    ...................................................................

    Using "data" mode will create the following directory that contains
    the pre-computed sequences for train, development, and test subsets:

        <root_dir>/<duration>+<step>/sequences/<database.task.protocol>.{train|development|test}.h5

    This means that <duration>-long sequences were generated with a step of
    <step> seconds, from the <database.task.protocol> protocol. This directory
    is called <data_dir> in the subsequent modes.

"train" mode:

    The configuration of each experiment is described in a file called
    <data_dir>/<xp_id>/config.yml, that describes the architecture of the
    neural network, and the approach (e.g. triplet loss) used for training the
    network:

    ................... <train_dir>/config.yml ...................
    architecture:
       name: TristouNet
       params:
         lstm: [16]
         mlp: [16, 16]
         bidirectional: concat

    approach:
       name: TripletLoss
       params:
         per_label: 2
         per_fold: 10
    ...................................................................

    Using "train" mode will create the following directory that contains a
    bunch of files including the pre-trained neural network weights after each
    epoch:

        <data_dir>/<xp_id>/train/<database.task.protocol>.<subset>

    This means that the network was trained using the <subset> subset of the
    <database.task.protocol> protocol, using the configuration described in
    <data_dir>/<xp_id>/config.yml. This directory  is called <train_dir> in the
    subsequent modes.

"validate" mode:
    Use the "validate" mode to run validation in parallel to training.
    "validate" mode will watch the <train_dir> directory, and run validation
    experiments every time a new epoch has ended. This will create the
    following directory that contains validation results:

        <train_dir>/validate/<database.task.protocol>

    You can run multiple "validate" in parallel (e.g. for every subset,
    protocol, task, or database).
"""

from os.path import dirname, basename, expanduser, isfile
import numpy as np
import pandas as pd
import yaml
from tqdm import tqdm
from docopt import docopt

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from pyannote.database import get_protocol
from pyannote.database import get_unique_identifier
from pyannote.audio.util import mkdir_p
import h5py

from .base import Application

from pyannote.generators.fragment import SlidingLabeledSegments
from pyannote.audio.optimizers import SSMORMS3

from pyannote.audio.embedding.utils import pdist, cdist, l2_normalize
from pyannote.metrics.binary_classification import det_curve

from sortedcontainers import SortedDict
from pyannote.audio.features import Precomputed
from pyannote.audio.embedding.extraction import SequenceEmbedding


class SpeakerEmbedding(Application):

    # created by "data" mode
    DATA_DIR = '{root_dir}/{params}'
    DATA_H5 = '{data_dir}/sequences/{protocol}.{subset}.h5'

    # created by "train" mode
    TRAIN_DIR = '{experiment_dir}/train/{protocol}.{subset}'
    NORM_H5 = '{train_dir}/norm.h5'

    @classmethod
    def from_root_dir(cls, root_dir, db_yml=None):
        speaker_embedding = cls(root_dir, db_yml=db_yml)
        speaker_embedding.root_dir_ = root_dir
        return speaker_embedding

    @classmethod
    def from_train_dir(cls, train_dir, db_yml=None):
        experiment_dir = dirname(dirname(train_dir))
        speaker_embedding = cls(experiment_dir, db_yml=db_yml)
        speaker_embedding.train_dir_ = train_dir

        root_dir = dirname(dirname(experiment_dir))
        with open(root_dir + '/config.yml', 'r') as fp:
            config = yaml.load(fp)
        extraction_name = config['feature_extraction']['name']
        features = __import__('pyannote.audio.features',
                              fromlist=[extraction_name])
        FeatureExtraction = getattr(features, extraction_name)

        params = config['feature_extraction'].get('params', {})

        if speaker_embedding.normalize_:
            norm_h5 = speaker_embedding.NORM_H5.format(
                train_dir=speaker_embedding.train_dir_)
            with h5py.File(norm_h5, mode='r') as g:
                params['mu'] = g.attrs['mu']
                params['sigma'] = g.attrs['sigma']

        speaker_embedding.feature_extraction_ = FeatureExtraction(**params)

        # do not cache features in memory when they are precomputed on disk
        # as this does not bring any significant speed-up
        # but does consume (potentially) a LOT of memory
        speaker_embedding.cache_preprocessed_ = 'Precomputed' not in extraction_name

        return speaker_embedding

    @classmethod
    def from_validate_txt(cls, validate_txt, db_yml=None):
        train_dir = dirname(dirname(dirname(validate_txt)))
        speaker_embedding = cls.from_train_dir(train_dir, db_yml=db_yml)
        speaker_embedding.validate_txt_ = validate_txt
        return speaker_embedding

    def __init__(self, experiment_dir, db_yml=None):

        super(SpeakerEmbedding, self).__init__(
            experiment_dir, db_yml=db_yml, backend='keras')

        # architecture
        if 'architecture' in self.config_:
            architecture_name = self.config_['architecture']['name']
            models = __import__('pyannote.audio.embedding.models_keras',
                                fromlist=[architecture_name])
            Architecture = getattr(models, architecture_name)
            self.architecture_ = Architecture(
                **self.config_['architecture'].get('params', {}))

        # approach
        if 'approach' in self.config_:
            approach_name = self.config_['approach']['name']
            approaches = __import__('pyannote.audio.embedding.approaches_keras',
                                    fromlist=[approach_name])
            Approach = getattr(approaches, approach_name)
            self.approach_ = Approach(
                **self.config_['approach'].get('params', {}))

        # feature normalization
        self.normalize_ = self.config_.get('normalize', False)

    # (5, None, None, False) ==> '5'
    # (5, 1, None, False) ==> '1-5'
    # (5, None, 2, False) ==> '5+2'
    # (5, 1, 2, False) ==> '1-5+2'
    # (5, None, None, True) ==> '5x'
    @staticmethod
    def _params_to_directory(duration=5.0, min_duration=None, step=None,
                            heterogeneous=False, skip_unlabeled=True,
                            **kwargs):
        if not skip_unlabeled:
            raise NotImplementedError('skip_unlabeled not supported yet.')

        DIRECTORY = '' if min_duration is None else '{min_duration:g}-'
        DIRECTORY += '{duration:g}'
        if step is not None:
            DIRECTORY += '+{step:g}'
        if heterogeneous:
            DIRECTORY += 'x'
        return DIRECTORY.format(duration=duration,
                                min_duration=min_duration,
                                step=step)

    # (5, None, None, False) <== '5'
    # (5, 1, None, False) <== '1-5'
    # (5, None, 2, False) <== '5+2'
    # (5, 1, 2, False) <== '1-5+2'
    @staticmethod
    def _directory_to_params(directory):
        heterogeneous = False
        if directory[-1] == 'x':
            heterogeneous = True
            directory = directory[:-1]
        tokens = directory.split('+')
        step = float(tokens[1]) if len(tokens) == 2 else None
        tokens = tokens[0].split('-')
        min_duration = float(tokens[0]) if len(tokens) == 2 else None
        duration = float(tokens[0]) if len(tokens) == 1 else float(tokens[1])
        return duration, min_duration, step, heterogeneous

    def data(self, protocol_name, duration=3.2, min_duration=None, step=None,
             heterogeneous=False):

        # labeled segment generator
        generator = SlidingLabeledSegments(duration=duration,
                                           min_duration=min_duration,
                                           step=step,
                                           heterogeneous=heterogeneous,
                                           source='annotated')

        data_dir = self.DATA_DIR.format(
            root_dir=self.root_dir_,
            params=self._params_to_directory(duration=duration,
                                            min_duration=min_duration,
                                            step=step,
                                            heterogeneous=heterogeneous))

        # file generator
        protocol = get_protocol(protocol_name, progress=True,
                                preprocessors=self.preprocessors_)

        for subset in ['train', 'development', 'test']:

            try:
                file_generator = getattr(protocol, subset)()
                first_item = next(file_generator)
            except NotImplementedError as e:
                continue

            file_generator = getattr(protocol, subset)()

            data_h5 = self.DATA_H5.format(data_dir=data_dir,
                                          protocol=protocol_name,
                                          subset=subset)
            mkdir_p(dirname(data_h5))

            with h5py.File(data_h5, mode='w') as fp:

                # initialize with a fixed number of sequences
                n_sequences = 1000

                # dataset meant to store the speaker identifier
                Y = fp.create_dataset(
                    'y', shape=(n_sequences, ),
                    dtype=h5py.special_dtype(vlen=bytes),
                    maxshape=(None, ))

                # dataset meant to store the speech turn unique ID
                Z = fp.create_dataset(
                    'z', shape=(n_sequences, ),
                    dtype=np.int64,
                    maxshape=(None, ))

                i = 0  # number of sequences
                z = 0  # speech turn identifier

                for item in file_generator:

                    # feature extraction
                    features = self.feature_extraction_(item)

                    for segment, y in generator.from_file(item):

                        # extract feature sequence
                        x = features.crop(segment,
                                          mode='center',
                                          fixed=duration)

                        # create X dataset to store feature sequences
                        # this cannot be done before because we need
                        # the number of samples per sequence and the
                        # dimension of feature vectors.
                        if i == 0:
                            # get number of samples and feature dimension
                            # from the first sequence...
                            n_samples, n_features = x.shape

                            # create X dataset accordingly
                            X = fp.create_dataset(
                                'X', dtype=x.dtype, compression='gzip',
                                shape=(n_sequences, n_samples, n_features),
                                chunks=(1, n_samples, n_features),
                                maxshape=(None, n_samples, n_features))

                            # make sure the speech turn identifier
                            # will not be erroneously incremented
                            prev_y = y

                        # increase the size of the datasets when full
                        if i == n_sequences:
                            n_sequences = int(n_sequences * 1.1)
                            X.resize(n_sequences, axis=0)
                            Y.resize(n_sequences, axis=0)
                            Z.resize(n_sequences, axis=0)

                        # save current feature sequence and its label
                        X[i] = x
                        Y[i] = y

                        # a change of label indicates that a new speech turn has began.
                        # increment speech turn identifier (z) accordingly
                        if y != prev_y:
                            prev_y = y
                            z += 1

                        # save speech turn identifier
                        Z[i] = z

                        # increment number of sequences
                        i += 1

                X.resize(i-1, axis=0)
                Y.resize(i-1, axis=0)
                Z.resize(i-1, axis=0)

                # precompute feature normalization
                weights, means, squared_means = zip(*(
                    (len(x), np.mean(x, axis=0), np.mean(x**2, axis=0))
                    for x in tqdm(X)))
                mu = np.average(means, weights=weights, axis=0)
                squared_mean = np.average(squared_means, weights=weights, axis=0)
                sigma = np.sqrt(squared_mean - mu ** 2)

                # store it as X attribute
                X.attrs['mu'] = mu
                X.attrs['sigma'] = sigma


    def train(self, protocol_name, subset='train', start=None, end=1000):

        train_dir = self.TRAIN_DIR.format(experiment_dir=self.experiment_dir,
                                          protocol=protocol_name,
                                          subset=subset)

        data_dir = dirname(self.experiment_dir)
        data_h5 = self.DATA_H5.format(data_dir=data_dir,
                                      protocol=protocol_name,
                                      subset=subset)

        if self.normalize_:
            # copy mu/sigma from data_h5 to norm_h5
            norm_h5 = self.NORM_H5.format(train_dir=train_dir)
            mkdir_p(train_dir)
            with h5py.File(data_h5, mode='r') as f, \
                 h5py.File(norm_h5, mode='w') as g:
                g.attrs['mu'] = f['X'].attrs['mu']
                g.attrs['sigma'] = f['X'].attrs['sigma']

        # generator
        got = self.approach_.get_batch_generator(
            data_h5, normalize=self.normalize_)
        batch_generator = got['batch_generator']
        batches_per_epoch = got['batches_per_epoch']
        n_classes = got.get('n_classes', None)
        classes = got.get('classes', None)

        if start is None:
            init_embedding = self.architecture_
        else:
            init_embedding = self.approach_.load(train_dir, start)

        self.approach_.fit(init_embedding, batch_generator,
                           batches_per_epoch=batches_per_epoch,
                           n_classes=n_classes, classes=classes,
                           epochs=end, log_dir=train_dir,
                           optimizer=SSMORMS3())

    def validate_init(self, protocol_name, subset='development'):

        task = protocol_name.split('.')[1]
        if task == 'SpeakerVerification':
            return self._validate_init_verification(protocol_name,
                                                    subset=subset)

        return self._validate_init_default(protocol_name, subset=subset)

    def _validate_init_verification(self, protocol_name, subset='development'):
        return {}

    def _validate_init_default(self, protocol_name, subset='development'):

        # reproducibility
        np.random.seed(1337)

        data_dir = dirname(dirname(dirname(self.train_dir_)))
        data_h5 = self.DATA_H5.format(data_dir=data_dir,
                                      protocol=protocol_name,
                                      subset=subset)

        with h5py.File(data_h5, mode='r') as fp:

            h5_X = fp['X']
            h5_y = fp['y']
            h5_z = fp['z']

            # group sequences by z
            df = pd.DataFrame({'y': h5_y, 'z': h5_z})
            z_groups = df.groupby('z')

            # label of each group
            y_groups = [group.y.iloc[0] for _, group in z_groups]

            # randomly select (at most) 10 groups from each speaker to ensure
            # all speakers have the same importance in the evaluation
            unique, y, counts = np.unique(y_groups, return_inverse=True,
                                          return_counts=True)
            n_speakers = len(unique)
            XX, X, N, Y = [], [], [], []
            for speaker in range(n_speakers):
                I = np.random.choice(np.where(y == speaker)[0],
                                     size=min(10, counts[speaker]),
                                     replace=False)
                for i in I:
                    selector = z_groups.get_group(i).index
                    x = h5_X[selector]
                    XX.append(x)
                    X.append(x[len(x) // 2])
                    N.append(x.shape[0])
                    Y.append(y[i])

        return {'XX': np.vstack(XX),
                'X': np.stack(X),
                'n': np.array(N),
                'y': np.array(Y)[:, np.newaxis]}

    def validate_epoch(self, epoch, protocol_name, subset='development',
                       validation_data=None):

        task = protocol_name.split('.')[1]
        if task == 'SpeakerVerification':
            return self._validate_epoch_verification(
                epoch, protocol_name, subset=subset,
                validation_data=validation_data)

        return self._validate_epoch_default(epoch, protocol_name, subset=subset,
                                           validation_data=validation_data)

    def _validate_epoch_verification(self, epoch, protocol_name,
                                     subset='development',
                                     validation_data=None):

        # load current model
        model = self.load_model(epoch)

        # guess sequence duration from path (.../3.2+0.8/...)
        directory = basename(dirname(self.experiment_dir))
        duration, _, step, _ = self._directory_to_params(directory)
        if step is None:
            step = 0.5 * duration

        # initialize embedding extraction
        batch_size = self.approach_.batch_size

        try:
            # use internal representation when available
            internal = True
            sequence_embedding = SequenceEmbedding(
                model, self.feature_extraction_, duration,
                step=step, batch_size=batch_size,
                internal=internal)

        except ValueError as e:
            # else use final representation
            internal = False
            sequence_embedding = SequenceEmbedding(
                model, self.feature_extraction_, duration,
                step=step, batch_size=batch_size,
                internal=internal)

        metrics = {}
        protocol = get_protocol(protocol_name, progress=False,
                                preprocessors=self.preprocessors_)

        enrolment_models, enrolment_khashes = {}, {}
        enrolments = getattr(protocol, '{0}_enrolment'.format(subset))()
        for i, enrolment in enumerate(enrolments):
            model_id = enrolment['model_id']
            embedding = sequence_embedding.apply(enrolment)
            data = embedding.crop(enrolment['enrol_with'],
                                  mode='center', return_data=True)
            enrolment_models[model_id] = np.mean(data, axis=0, keepdims=True)

            # in some specific speaker verification protocols,
            # enrolment data may be  used later as trial data.
            # therefore, we cache information about enrolment data
            # to speed things up by reusing the enrolment as trial
            h = hash((get_unique_identifier(enrolment),
                      tuple(enrolment['enrol_with'])))
            enrolment_khashes[h] = model_id

        trial_models = {}
        trials = getattr(protocol, '{0}_trial'.format(subset))()
        y_true, y_pred = [], []
        for i, trial in enumerate(trials):
            model_id = trial['model_id']

            h = hash((get_unique_identifier(trial),
                      tuple(trial['try_with'])))

            # re-use enrolment model whenever possible
            if h in enrolment_khashes:
                model = enrolment_models[enrolment_khashes[h]]

            # re-use trial model whenever possible
            elif h in trial_models:
                model = trial_models[h]

            else:
                embedding = sequence_embedding.apply(trial)
                data = embedding.crop(trial['try_with'],
                                      mode='center', return_data=True)
                model = np.mean(data, axis=0, keepdims=True)
                # cache trial model for later re-use
                trial_models[h] = model

            distance = cdist(enrolment_models[model_id], model,
                             metric=self.approach_.metric)[0, 0]
            y_pred.append(distance)
            y_true.append(trial['reference'])

        _, _, _, eer = det_curve(np.array(y_true), np.array(y_pred),
                                 distances=True)
        metrics['EER.internal' if internal else 'EER.final'] = \
            {'minimize': True, 'value': eer}

        return metrics


    def _validate_epoch_default(self, epoch, protocol_name,
                               subset='development', validation_data=None):

        from pyannote.core.util import pairwise
        import keras.backend as K
        batch_size = 2048

        metrics = {}

        # compute pairwise groundtruth
        y = validation_data['y']
        y_true = pdist(y, metric='chebyshev') < 1

        # load embedding
        embedding = self.load_model(epoch)
        X = validation_data['X']
        fX = embedding.predict(X, batch_size=batch_size)
        y_pred = pdist(fX, metric=self.approach_.metric)
        _, _, _, eer = det_curve(y_true, y_pred, distances=True)
        metrics['EER.1seq'] = {'minimize': True, 'value': eer}

        try:
            internal_layer = embedding.get_layer(name='internal')
        except ValueError as e:
            return metrics

        # internal embeddings
        def embed(XX):
            func = K.function(
                [embedding.get_layer(name='input').input, K.learning_phase()],
                [internal_layer.output])
            return func([XX, 0])[0]

        # embed internal embedding using batches
        XX = validation_data['XX']
        # `batches` is meant to contain batches boundaries
        batches = list(np.arange(0, len(XX), batch_size))
        if len(XX) % batch_size:
            batches += [len(XX)]
        fX = np.vstack(embed(XX[i:j]) for i, j in pairwise(batches))

        # sum of all internal embeddings of each group
        indices = np.hstack([[0], np.cumsum(validation_data['n'])])
        fX = np.stack([np.sum(np.sum(fX[i:j], axis=0), axis=0)
                                for i, j in pairwise(indices)])
        fX = l2_normalize(fX)

        y_pred = pdist(fX, metric=self.approach_.metric)
        _, _, _, eer = det_curve(y_true, y_pred, distances=True)
        metrics['EER.Xseq'] = {'minimize': True, 'value': eer}

        return metrics

    def apply(self, protocol_name, output_dir, step=None, internal=False):

        # load best performing model
        with open(self.validate_txt_, 'r') as fp:
            eers = SortedDict(np.loadtxt(fp))
        best_epoch = int(eers.iloc[np.argmin(eers.values())])
        model = self.load_model(best_epoch)

        # guess sequence duration from path (.../3.2+0.8/...)
        directory = basename(dirname(self.experiment_dir))
        duration, _, _, _ = self._directory_to_params(directory)
        if step is None:
            step = 0.5 * duration

        # initialize embedding extraction
        batch_size = self.approach_.batch_size
        sequence_embedding = SequenceEmbedding(
            model, self.feature_extraction_, duration,
            step=step, batch_size=batch_size,
            internal=internal)
        sliding_window = sequence_embedding.sliding_window
        dimension = sequence_embedding.dimension

        # create metadata file at root that contains
        # sliding window and dimension information
        precomputed = Precomputed(
            root_dir=output_dir,
            sliding_window=sliding_window,
            dimension=dimension)

        # file generator
        protocol = get_protocol(protocol_name, progress=True,
                                preprocessors=self.preprocessors_)

        processed_uris = set()

        for subset in ['development', 'test', 'train']:

            try:
                file_generator = getattr(protocol, subset)()
                first_item = next(file_generator)
            except NotImplementedError as e:
                continue

            file_generator = getattr(protocol, subset)()

            for current_file in file_generator:

                # corner case when the same file is iterated several times
                uri = get_unique_identifier(current_file)
                if uri in processed_uris:
                    continue

                fX = sequence_embedding.apply(current_file)

                precomputed.dump(current_file, fX)
                processed_uris.add(uri)

def main():

    arguments = docopt(__doc__, version='Speaker embedding')

    db_yml = expanduser(arguments['--database'])
    protocol_name = arguments['<database.task.protocol>']
    subset = arguments['--subset']

    if arguments['data']:

        duration = float(arguments['--duration'])

        step = arguments['--step']
        if step is not None:
            step = float(step)

        heterogeneous = arguments['--heterogeneous']

        root_dir = arguments['<root_dir>']
        if subset is None:
            subset = 'train'

        application = SpeakerEmbedding.from_root_dir(root_dir, db_yml=db_yml)
        application.data(protocol_name, duration=duration, step=step,
                         heterogeneous=heterogeneous)

    if arguments['train']:
        experiment_dir = arguments['<experiment_dir>']

        if subset is None:
            subset = 'train'

        start = arguments['--start']
        if start is not None:
            start = int(start)

        end = int(arguments['--end'])

        application = SpeakerEmbedding(experiment_dir)
        application.train(protocol_name, subset=subset, start=start, end=end)

    if arguments['validate']:
        train_dir = arguments['<train_dir>']

        if subset is None:
            subset = 'development'

        every = int(arguments['--every'])
        start = int(arguments['--from'])
        end = arguments['--to']
        if end is not None:
            end = int(end)

        application = SpeakerEmbedding.from_train_dir(train_dir)
        application.validate(protocol_name, subset=subset,
                             every=every, start=start, end=end)

    if arguments['apply']:
        # [0] is a hack due to a bug in docopt and
        # "compare" mode support for multiple <validate.txt>
        validate_txt = arguments['<validate.txt>'][0]
        output_dir = arguments['<output_dir>']
        if subset is None:
            subset = 'test'

        step = arguments['--step']
        if step is not None:
            step = float(step)

        internal = arguments['--internal']

        application = SpeakerEmbedding.from_validate_txt(validate_txt)
        application.apply(protocol_name, output_dir, step=step,
                          internal=internal)

    if arguments['compare']:

        from pandas import read_table
        from pandas import concat
        from datetime import datetime

        to_timestamp = \
            lambda t: datetime.strptime(t, '%Y-%m-%dT%H:%M:%S.%f').timestamp()

        fig, ax = plt.subplots()

        for validate_txt, legend in zip(arguments['<validate.txt>'],
                                        arguments['<legend>']):

            # load logs
            eer = read_table(validate_txt, delim_whitespace=True,
                             names=['epoch', 'eer'], index_col=['epoch'])
            eer = eer.loc[~eer.index.duplicated(keep='first')]

            app = SpeakerEmbedding.from_validate_txt(validate_txt)
            train_dir = app.train_dir_
            loss_txt = '{train_dir}/loss.train.txt'.format(train_dir=train_dir)
            loss = read_table(loss_txt, delim_whitespace=True,
                              names=['epoch', 't', 'loss'],
                              index_col=['epoch'],
                              converters={'t': to_timestamp})
            loss['t'] = loss['t'] - loss['t'].iloc[0]

            # plot logs
            data = concat([eer, loss], axis=1)
            data.dropna(inplace=True)
            data.sort_index(inplace=True)
            ax.plot(data['t'] / 3600, 100 * data['eer'], label=legend)

        ax.set_ylabel('EER (%)')
        ax.set_xlabel('time (hours)')
        ax.legend()

        fig.savefig(arguments['<output.png>'])