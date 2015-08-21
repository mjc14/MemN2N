from __future__ import division
import argparse
import glob
import lasagne
import nltk
import numpy as np
import sys
import theano
import theano.tensor as T
import time
from sklearn import metrics
from sklearn.preprocessing import LabelBinarizer
from theano.printing import Print as pp

import warnings
warnings.filterwarnings('ignore', '.*topo.*')


class InnerProductLayer(lasagne.layers.MergeLayer):

    def __init__(self, incomings, nonlinearity=None, **kwargs):
        super(InnerProductLayer, self).__init__(incomings, **kwargs)
        self.nonlinearity = nonlinearity
        if len(incomings) != 2:
            raise NotImplementedError

    def get_output_shape_for(self, input_shapes):
        return input_shapes[0][:2]

    def get_output_for(self, inputs, **kwargs):
        M = inputs[0]
        u = inputs[1]
        output = T.batched_dot(M, u)
        if self.nonlinearity is not None:
            output = self.nonlinearity(output)
        return output


class BatchedDotLayer(lasagne.layers.MergeLayer):

    def __init__(self, incomings, **kwargs):
        super(BatchedDotLayer, self).__init__(incomings, **kwargs)
        if len(incomings) != 2:
            raise NotImplementedError

    def get_output_shape_for(self, input_shapes):
        return (input_shapes[1][0], input_shapes[1][2])

    def get_output_for(self, inputs, **kwargs):
        return T.batched_dot(inputs[0], inputs[1])


class SumLayer(lasagne.layers.Layer):

    def __init__(self, incoming, axis, **kwargs):
        super(SumLayer, self).__init__(incoming, **kwargs)
        self.axis = axis

    def get_output_shape_for(self, input_shape):
        return input_shape[:self.axis] + input_shape[self.axis+1:]

    def get_output_for(self, input, **kwargs):
        return T.sum(input, axis=self.axis)


class TemporalEncodingLayer(lasagne.layers.Layer):

    def __init__(self, incoming, T=lasagne.init.Normal(std=0.1), **kwargs):
        super(TemporalEncodingLayer, self).__init__(incoming, **kwargs)
        self.T = self.add_param(T, self.input_shape[-2:], name="T")

    def get_output_shape_for(self, input_shape):
        return input_shape

    def get_output_for(self, input, **kwargs):
        return input + self.T


class TransposedDenseLayer(lasagne.layers.DenseLayer):

    def __init__(self, incoming, num_units, W=lasagne.init.GlorotUniform(),
                 b=lasagne.init.Constant(0.), nonlinearity=lasagne.nonlinearities.rectify,
                 **kwargs):
        super(TransposedDenseLayer, self).__init__(incoming, num_units, W, b, nonlinearity, **kwargs)

    def get_output_shape_for(self, input_shape):
        return (input_shape[0], self.num_units)

    def get_output_for(self, input, **kwargs):
        if input.ndim > 2:
            input = input.flatten(2)

        activation = T.dot(input, self.W.T)
        if self.b is not None:
            activation = activation + self.b.dimshuffle('x', 0)
        return self.nonlinearity(activation)


class RecurrentEncodingLayer(lasagne.layers.MergeLayer):

    def __init__(self, incomings, W_in_to_hid, W_hid_to_hid, **kwargs):
        super(RecurrentEncodingLayer, self).__init__(incomings, **kwargs)
        if len(incomings) != 2:
            raise NotImplementedError

        self.embedding_size = self.input_shapes[0][-1]
        l_in = lasagne.layers.InputLayer(shape=self.input_shapes[0])
        self.l_recurrent = lasagne.layers.RecurrentLayer(l_in,
                                                         self.embedding_size,
                                                         W_in_to_hid=W_in_to_hid,
                                                         W_hid_to_hid=W_hid_to_hid,
                                                         b=None,
                                                         nonlinearity=lasagne.nonlinearities.tanh)
#        self.network = lasagne.layers.DropoutLayer(self.l_recurrent, p=0.5)
        self.network = self.l_recurrent

        params = lasagne.layers.helper.get_all_params(self.network, trainable=True)
        values = lasagne.layers.helper.get_all_param_values(self.network, trainable=True)
        for p, v in zip(params, values):
            self.add_param(p, v.shape, name=p.name)

    def get_output_shape_for(self, input_shapes):
        return (input_shapes[0][0], input_shapes[0][2])

    def get_output_for(self, inputs, **kwargs):
        output = lasagne.layers.helper.get_output(self.network, inputs[0])
        return output[T.arange(self.input_shapes[0][0]), inputs[1].flatten()].reshape((self.input_shapes[0][0], self.embedding_size))


class MemoryNetworkLayer(lasagne.layers.MergeLayer):

    def __init__(self, incomings, vocab, embedding_size, A, C, A_Wi, A_Wh, C_Wi, C_Wh, nonlinearity=lasagne.nonlinearities.softmax, **kwargs):
        super(MemoryNetworkLayer, self).__init__(incomings, **kwargs)
        if len(incomings) != 3:
            raise NotImplementedError

        batch_size, max_seqlen, max_sentlen = self.input_shapes[0]

        l_context_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen))
        l_B_embedding = lasagne.layers.InputLayer(shape=(batch_size, embedding_size))
        self.l_context_seqlen_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen))

        l_context_in = lasagne.layers.ReshapeLayer(l_context_in, shape=(batch_size * max_seqlen * max_sentlen, ))
        l_A_embedding = lasagne.layers.EmbeddingLayer(l_context_in, len(vocab)+1, embedding_size, W=A)
        self.A = l_A_embedding.W
        l_A_embedding = lasagne.layers.ReshapeLayer(l_A_embedding, shape=(batch_size * max_seqlen, max_sentlen, embedding_size))
        l_A_embedding = RecurrentEncodingLayer((l_A_embedding, self.l_context_seqlen_in), W_in_to_hid=A_Wi, W_hid_to_hid=A_Wh)
        self.A_Wi, self.A_Wh = l_A_embedding.l_recurrent.W_in_to_hid, l_A_embedding.l_recurrent.W_hid_to_hid
        l_A_embedding = lasagne.layers.ReshapeLayer(l_A_embedding, shape=(batch_size, max_seqlen, embedding_size))

        l_C_embedding = lasagne.layers.EmbeddingLayer(l_context_in, len(vocab)+1, embedding_size, W=C)
        self.C = l_C_embedding.W
        l_C_embedding = lasagne.layers.ReshapeLayer(l_C_embedding, shape=(batch_size * max_seqlen, max_sentlen, embedding_size))
        l_C_embedding = RecurrentEncodingLayer((l_C_embedding, self.l_context_seqlen_in), W_in_to_hid=C_Wi, W_hid_to_hid=C_Wh)
        self.C_Wi, self.C_Wh = l_C_embedding.l_recurrent.W_in_to_hid, l_C_embedding.l_recurrent.W_hid_to_hid
        l_C_embedding = lasagne.layers.ReshapeLayer(l_C_embedding, shape=(batch_size, max_seqlen, embedding_size))

        l_prob = InnerProductLayer((l_A_embedding, l_B_embedding), nonlinearity=nonlinearity)
        l_weighted_output = BatchedDotLayer((l_prob, l_C_embedding))

        l_sum = lasagne.layers.ElemwiseSumLayer((l_weighted_output, l_B_embedding))

        self.l_context_in = l_context_in
        self.l_B_embedding = l_B_embedding
        self.network = l_sum

        params = lasagne.layers.helper.get_all_params(self.network, trainable=True)
        values = lasagne.layers.helper.get_all_param_values(self.network, trainable=True)
        print 'memlayer: ', params
        for p, v in zip(params, values):
            self.add_param(p, v.shape, name=p.name)

        zero_vec_tensor = T.vector()
        self.zero_vec = np.zeros(embedding_size, dtype=theano.config.floatX)
        self.set_zero = theano.function([zero_vec_tensor], updates=[(x, T.set_subtensor(x[0, :], zero_vec_tensor)) for x in [self.A, self.C]])

    def get_output_shape_for(self, input_shapes):
        return self.network.get_output_shape()

    def get_output_for(self, inputs, **kwargs):
        return lasagne.layers.helper.get_output(self.network, {self.l_context_in: inputs[0], self.l_B_embedding: inputs[1], self.l_context_seqlen_in: inputs[2]})

    def reset_zero(self):
        self.set_zero(self.zero_vec)


class Model:

    def __init__(self, train_file, test_file, batch_size=32, embedding_size=20, max_norm=40, lr=0.01, num_hops=3, adj_weight_tying=True, linear_start=True, **kwargs):
        train_lines, test_lines = self.get_lines(train_file), self.get_lines(test_file)
        lines = np.concatenate([train_lines, test_lines], axis=0)
        vocab, word_to_idx, idx_to_word, max_seqlen, max_sentlen = self.get_vocab(lines)

        self.data = {'train': {}, 'test': {}}
        S_train, self.data['train']['C'], self.data['train']['Q'], self.data['train']['Y'] = self.process_dataset(train_lines, word_to_idx, max_sentlen, offset=0)
        S_test, self.data['test']['C'], self.data['test']['Q'], self.data['test']['Y'] = self.process_dataset(test_lines, word_to_idx, max_sentlen, offset=len(S_train))
        S = np.concatenate([np.zeros((1, max_sentlen), dtype=np.int32), S_train, S_test], axis=0)
        for i in range(10):
            for k in ['C', 'Q', 'Y']:
                print k, self.data['test'][k][i]
        print 'batch_size:', batch_size, 'max_seqlen:', max_seqlen, 'max_sentlen:', max_sentlen
        print 'sentences:', S.shape
        print 'vocab:', len(vocab), vocab
        for d in ['train', 'test']:
            print d,
            for k in ['C', 'Q', 'Y']:
                print k, self.data[d][k].shape,
            print ''

        lb = LabelBinarizer()
        lb.fit(list(vocab))
        vocab = lb.classes_.tolist()

        self.batch_size = batch_size
        self.max_seqlen = max_seqlen
        self.max_sentlen = max_sentlen
        self.embedding_size = embedding_size
        self.num_classes = len(vocab) + 1
        self.vocab = vocab
        self.adj_weight_tying = adj_weight_tying
        self.num_hops = num_hops
        self.lb = lb
        self.init_lr = lr
        self.lr = self.init_lr
        self.max_norm = max_norm
        self.S = S
        self.idx_to_word = idx_to_word
        self.nonlinearity = None if linear_start else lasagne.nonlinearities.softmax

        self.build_network(self.nonlinearity)

    def build_network(self, nonlinearity):
        batch_size, max_seqlen, max_sentlen, embedding_size, vocab = self.batch_size, self.max_seqlen, self.max_sentlen, self.embedding_size, self.vocab

        c = T.imatrix()
        q = T.ivector()
        y = T.imatrix()
        c_seqlen = T.imatrix()
        q_seqlen = T.imatrix()
        self.c_shared = theano.shared(np.zeros((batch_size, max_seqlen), dtype=np.int32), borrow=True)
        self.q_shared = theano.shared(np.zeros((batch_size, ), dtype=np.int32), borrow=True)
        self.a_shared = theano.shared(np.zeros((batch_size, self.num_classes), dtype=np.int32), borrow=True)
        self.c_seqlen_shared = theano.shared(np.zeros((batch_size, max_seqlen), dtype=np.int32), borrow=True)
        self.q_seqlen_shared = theano.shared(np.zeros((batch_size, 1), dtype=np.int32), borrow=True)
        S_shared = theano.shared(self.S, borrow=True)

        cc = S_shared[c.flatten()].reshape((batch_size, max_seqlen, max_sentlen))
        qq = S_shared[q.flatten()].reshape((batch_size, max_sentlen))

        l_context_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen))
        l_question_in = lasagne.layers.InputLayer(shape=(batch_size, max_sentlen))

        l_context_seqlen_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen))
        l_question_seqlen_in = lasagne.layers.InputLayer(shape=(batch_size, 1))

        A, C = lasagne.init.Normal(std=0.1).sample((len(vocab)+1, embedding_size)), lasagne.init.Normal(std=0.1)
        if self.adj_weight_tying:
            W = A
            W_in_to_hid = lasagne.init.Orthogonal().sample((embedding_size, embedding_size))
            W_hid_to_hid = lasagne.init.Orthogonal().sample((embedding_size, embedding_size))
        else:
            W = lasagne.init.Normal(std=0.1)
            W_in_to_hid = lasagne.init.Orthogonal()
            W_hid_to_hid = lasagne.init.Orthogonal()

        l_question_in = lasagne.layers.ReshapeLayer(l_question_in, shape=(batch_size * max_sentlen, ))
        l_B_embedding = lasagne.layers.EmbeddingLayer(l_question_in, len(vocab)+1, embedding_size, W=W)
        B = l_B_embedding.W
        l_B_embedding = lasagne.layers.ReshapeLayer(l_B_embedding, shape=(batch_size, max_sentlen, embedding_size))
        l_B_embedding = RecurrentEncodingLayer((l_B_embedding, l_question_seqlen_in), W_in_to_hid, W_hid_to_hid)

        self.mem_layers = [MemoryNetworkLayer((l_context_in, l_B_embedding, l_context_seqlen_in), vocab, embedding_size, A=A, C=C, A_Wi=W_in_to_hid, A_Wh=W_hid_to_hid, C_Wi=lasagne.init.Orthogonal(), C_Wh=lasagne.init.Orthogonal(), nonlinearity=nonlinearity)]
        for _ in range(1, self.num_hops):
            if self.adj_weight_tying:
                A, C = self.mem_layers[-1].C, lasagne.init.Normal(std=0.1)
                A_Wi, A_Wh = self.mem_layers[-1].C_Wi, self.mem_layers[-1].C_Wh
                C_Wi, C_Wh = lasagne.init.Orthogonal(), lasagne.init.Orthogonal()
            else:  # RNN style
                A, C = self.mem_layers[-1].A, self.mem_layers[-1].C
                A_Wi, A_Wh = self.mem_layers[-1].A_Wi, self.mem_layers[-1].A_Wh
                C_Wi, C_Wh = self.mem_layers[-1].C_Wi, self.mem_layers[-1].C_Wh
            self.mem_layers += [MemoryNetworkLayer((l_context_in, self.mem_layers[-1], l_context_seqlen_in), vocab, embedding_size, A=A, C=C, A_Wi=A_Wi, A_Wh=A_Wh, C_Wi=C_Wi, C_Wh=C_Wh, nonlinearity=nonlinearity)]

        if self.adj_weight_tying:
            l_pred = TransposedDenseLayer(self.mem_layers[-1], self.num_classes, W=self.mem_layers[-1].C, b=None, nonlinearity=lasagne.nonlinearities.softmax)
        else:
            l_pred = lasagne.layers.DenseLayer(self.mem_layers[-1], self.num_classes, W=lasagne.init.Normal(std=0.1), b=None, nonlinearity=lasagne.nonlinearities.softmax)

        probas = lasagne.layers.helper.get_output(l_pred, {l_context_in: cc, l_question_in: qq, l_context_seqlen_in: c_seqlen, l_question_seqlen_in: q_seqlen})
        probas = T.clip(probas, 1e-7, 1.0-1e-7)

        pred = T.argmax(probas, axis=1)

        cost = T.nnet.binary_crossentropy(probas, y).sum()

        params = lasagne.layers.helper.get_all_params(l_pred, trainable=True)
        print 'params:', params
        grads = T.grad(cost, params)
        scaled_grads = lasagne.updates.total_norm_constraint(grads, self.max_norm)
#        updates = lasagne.updates.sgd(scaled_grads, params, learning_rate=self.lr)
        updates = lasagne.updates.adam(scaled_grads, params, 0.001)

        givens = {
            c: self.c_shared,
            q: self.q_shared,
            y: self.a_shared,
            c_seqlen: self.c_seqlen_shared,
            q_seqlen: self.q_seqlen_shared
        }

        self.train_model = theano.function([], cost, givens=givens, updates=updates)
        self.compute_pred = theano.function([], pred, givens=givens, on_unused_input='ignore')

        zero_vec_tensor = T.vector()
        self.zero_vec = np.zeros(embedding_size, dtype=theano.config.floatX)
        self.set_zero = theano.function([zero_vec_tensor], updates=[(x, T.set_subtensor(x[0, :], zero_vec_tensor)) for x in [B]])

        self.nonlinearity = nonlinearity
        self.network = l_pred

    def reset_zero(self):
        self.set_zero(self.zero_vec)
        for l in self.mem_layers:
            l.reset_zero()

    def predict(self, dataset, index):
        self.set_shared_variables(dataset, index)
        return self.compute_pred()

    def compute_f1(self, dataset):
        n_batches = len(dataset['Y']) // self.batch_size
        y_pred = np.concatenate([self.predict(dataset, i) for i in xrange(n_batches)]).astype(np.int32) - 1
        y_true = [self.vocab.index(y) for y in dataset['Y'][:len(y_pred)]]
        print metrics.confusion_matrix(y_true, y_pred)
        print metrics.classification_report(y_true, y_pred)
        errors = []
        for i, (t, p) in enumerate(zip(y_true, y_pred)):
            if t != p:
                errors.append((i, self.lb.classes_[p]))
        return metrics.f1_score(y_true, y_pred, average='weighted', pos_label=None), errors

    def train(self, n_epochs=100, shuffle_batch=False):
        epoch = 0
        n_train_batches = len(self.data['train']['Y']) // self.batch_size
        self.lr = self.init_lr
        prev_train_f1 = None

        while (epoch < n_epochs):
            epoch += 1

            if epoch % 25 == 0:
                self.lr /= 2.0

            indices = range(n_train_batches)
            if shuffle_batch:
                self.shuffle_sync(self.data['train'])

            total_cost = 0
            start_time = time.time()
            for minibatch_index in indices:
                self.set_shared_variables(self.data['train'], minibatch_index)
                total_cost += self.train_model()
                self.reset_zero()
            end_time = time.time()
            print '\n' * 3, '*' * 80
            print 'epoch:', epoch, 'cost:', (total_cost / len(indices)), ' took: %d(s)' % (end_time - start_time)

            print 'TRAIN', '=' * 40
            train_f1, train_errors = self.compute_f1(self.data['train'])
            print 'TRAIN_ERROR:', (1-train_f1)*100
            if False:
                for i, pred in train_errors[:10]:
                    print 'context: ', self.to_words(self.data['train']['C'][i])
                    print 'question: ', self.to_words([self.data['train']['Q'][i]])
                    print 'correct answer: ', self.data['train']['Y'][i]
                    print 'predicted answer: ', pred
                    print '---' * 20

            if prev_train_f1 is not None and train_f1 < prev_train_f1 and self.nonlinearity is None:
                prev_weights = lasagne.layers.helper.get_all_param_values(self.network)
                self.build_network(nonlinearity=lasagne.nonlinearities.softmax)
                lasagne.layers.helper.set_all_param_values(self.network, prev_weights)
            else:
                print 'TEST', '=' * 40
                test_f1, test_errors = self.compute_f1(self.data['test'])
                print '*** TEST_ERROR:', (1-test_f1)*100

            prev_train_f1 = train_f1

    def to_words(self, indices):
        sents = []
        for idx in indices:
            words = ' '.join([self.idx_to_word[idx] for idx in self.S[idx] if idx > 0])
            sents.append(words)
        return ' '.join(sents)

    def shuffle_sync(self, dataset):
        p = np.random.permutation(len(dataset['Y']))
        for k in ['C', 'Q', 'Y']:
            dataset[k] = dataset[k][p]

    def set_shared_variables(self, dataset, index):
        c = np.zeros((self.batch_size, self.max_seqlen), dtype=np.int32)
        q = np.zeros((self.batch_size, ), dtype=np.int32)
        y = np.zeros((self.batch_size, self.num_classes), dtype=np.int32)
        c_seqlen = np.zeros((self.batch_size, self.max_seqlen), dtype=np.int32)
        q_seqlen = np.zeros((self.batch_size, 1), dtype=np.int32)

        indices = range(index*self.batch_size, (index+1)*self.batch_size)
        for i, row in enumerate(dataset['C'][indices]):
            row = row[:self.max_seqlen]
            c[i, :len(row)] = row

        q[:len(indices)] = dataset['Q'][indices]

        for key, seqlen in [('C', c_seqlen), ('Q', q_seqlen)]:
            for i, row in enumerate(dataset[key][indices]):
                sentences = self.S[row].reshape((-1, self.max_sentlen))
                for ii, word_idxs in enumerate(sentences):
                    J = np.count_nonzero(word_idxs)
                    seqlen[i, ii] = J - 1

        y[:len(indices), 1:self.num_classes] = self.lb.transform(dataset['Y'][indices])

        self.c_shared.set_value(c)
        self.q_shared.set_value(q)
        self.a_shared.set_value(y)
        self.c_seqlen_shared.set_value(c_seqlen)
        self.q_seqlen_shared.set_value(q_seqlen)

    def get_vocab(self, lines):
        vocab = set()
        max_sentlen = 0
        for i, line in enumerate(lines):
            words = nltk.word_tokenize(line['text'])
            max_sentlen = max(max_sentlen, len(words))
            for w in words:
                vocab.add(w)
            if line['type'] == 'q':
                vocab.add(line['answer'])

        word_to_idx = {}
        for w in vocab:
            word_to_idx[w] = len(word_to_idx) + 1

        idx_to_word = {}
        for w, idx in word_to_idx.iteritems():
            idx_to_word[idx] = w

        max_seqlen = 0
        for i, line in enumerate(lines):
            if line['type'] == 'q':
                id = line['id']-1
                indices = [idx for idx in range(i-id, i) if lines[idx]['type'] == 's'][::-1][:50]
                max_seqlen = max(len(indices), max_seqlen)

        return vocab, word_to_idx, idx_to_word, max_seqlen, max_sentlen

    def process_dataset(self, lines, word_to_idx, max_sentlen, offset):
        S, C, Q, Y = [], [], [], []

        for i, line in enumerate(lines):
            word_indices = [word_to_idx[w] for w in nltk.word_tokenize(line['text'])]
            word_indices += [0] * (max_sentlen - len(word_indices))
            S.append(word_indices)
            if line['type'] == 'q':
                id = line['id']-1
                indices = [offset+idx+1 for idx in range(i-id, i) if lines[idx]['type'] == 's'][::-1][:50]
                line['refs'] = [indices.index(offset+i+1-id+ref) for ref in line['refs']]
                C.append(indices)
                Q.append(offset+i+1)
                Y.append(line['answer'])
        return np.array(S, dtype=np.int32), np.array(C), np.array(Q, dtype=np.int32), np.array(Y)

    def get_lines(self, fname):
        lines = []
        for i, line in enumerate(open(fname)):
            id = int(line[0:line.find(' ')])
            line = line.strip()
            line = line[line.find(' ')+1:]
            if line.find('?') == -1:
                lines.append({'type': 's', 'text': line})
            else:
                idx = line.find('?')
                tmp = line[idx+1:].split('\t')
                lines.append({'id': id, 'type': 'q', 'text': line[:idx], 'answer': tmp[1].strip(), 'refs': [int(x)-1 for x in tmp[2:][0].split(' ')]})
            if False and i > 1000:
                break
        return np.array(lines)


def str2bool(v):
    return v.lower() in ('yes', 'true', 't', '1')


def main():
    parser = argparse.ArgumentParser()
    parser.register('type', 'bool', str2bool)
    parser.add_argument('--task', type=int, default=1, help='Task#')
    parser.add_argument('--train_file', type=str, default='', help='Train file')
    parser.add_argument('--test_file', type=str, default='', help='Test file')
    parser.add_argument('--batch_size', type=int, default=32, help='Batch size')
    parser.add_argument('--embedding_size', type=int, default=20, help='Embedding size')
    parser.add_argument('--max_norm', type=float, default=40.0, help='Max norm')
    parser.add_argument('--lr', type=float, default=0.01, help='Learning rate')
    parser.add_argument('--num_hops', type=int, default=3, help='Num hops')
    parser.add_argument('--adj_weight_tying', type='bool', default=True, help='Whether to use adjacent weight tying')
    parser.add_argument('--linear_start', type='bool', default=False, help='Whether to start with linear activations')
    parser.add_argument('--shuffle_batch', type='bool', default=True, help='Whether to shuffle minibatches')
    parser.add_argument('--n_epochs', type=int, default=100, help='Num epochs')
    args = parser.parse_args()
    print '*' * 80
    print 'args:', args
    print '*' * 80

    if args.train_file == '' or args.test_file == '':
        args.train_file = glob.glob('data/en-10k/qa%d_*train.txt' % args.task)[0]
        args.test_file = glob.glob('data/en-10k/qa%d_*test.txt' % args.task)[0]

    model = Model(**args.__dict__)
    model.train(n_epochs=args.n_epochs, shuffle_batch=args.shuffle_batch)

if __name__ == '__main__':
    main()
