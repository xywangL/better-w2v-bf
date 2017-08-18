# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Multi-threaded word2vec mini-batched skip-gram model.

Trains the model described in:
(Mikolov, et. al.) Efficient Estimation of Word Representations in Vector Space
ICLR 2013.
http://arxiv.org/abs/1301.3781
This model does traditional minibatching.

The key ops used are:
* placeholder for feeding in tensors for each example.
* embedding_lookup for fetching rows from the embedding matrix.
* sigmoid_cross_entropy_with_logits to calculate the loss.
* GradientDescentOptimizer for optimizing the loss.
* skipgram custom op that does input processing.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import threading
import time

from six.moves import xrange  # pylint: disable=redefined-builtin

import numpy as np
import tensorflow as tf
from bf import bloomfilter

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"]="2"

word2vec = tf.load_op_library(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'word2vec_ops.so'))

flags = tf.app.flags

flags.DEFINE_string("save_path", None, "Directory to write the model and "
                    "training summaries.")
flags.DEFINE_string("train_data", None, "Training text file. "
                    "E.g., unzipped file http://mattmahoney.net/dc/text8.zip.")
flags.DEFINE_string("plk_table", None, "Pickle file which generated by tohash.")
flags.DEFINE_string(
    "eval_data", None, "File consisting of analogies of four tokens."
    "embedding 2 - embedding 1 + embedding 3 should be close "
    "to embedding 4."
    "See README.md for how to get 'questions-words.txt'.")
flags.DEFINE_integer("embedding_size", 200, "The embedding dimension size.")
flags.DEFINE_integer("num_hash_func", 7, "Number of hash functions.")
flags.DEFINE_integer(
    "epochs_to_train", 15,
    "Number of epochs to train. Each epoch processes the training data once "
    "completely.")
flags.DEFINE_float("learning_rate", 0.2, "Initial learning rate.")
flags.DEFINE_integer("num_neg_samples", 100,
                     "Negative samples per training example.")
flags.DEFINE_integer("batch_size", 16,
                     "Number of training examples processed per step "
                     "(size of a minibatch).")
flags.DEFINE_integer("concurrent_steps", 12,
                     "The number of concurrent training steps.")
flags.DEFINE_integer("window_size", 5,
                     "The number of words to predict to the left and right "
                     "of the target word.")
flags.DEFINE_integer("min_count", 5,
                     "The minimum number of word occurrences for it to be "
                     "included in the vocabulary.")
flags.DEFINE_integer("hash_func_max", 65535,
                     "The maximum value of hash function output.")
flags.DEFINE_float("subsample", 1e-3,
                   "Subsample threshold for word occurrence. Words that appear "
                   "with higher frequency will be randomly down-sampled. Set "
                   "to 0 to disable.")

flags.DEFINE_boolean(
    "restore_model", True,
    "If true, restore the model in saved_path and continue the training.")
flags.DEFINE_boolean(
    "interactive", False,
    "If true, enters an IPython interactive session to play with the trained "
    "model. E.g., try model.analogy(b'france', b'paris', b'russia') and "
    "model.nearby([b'proton', b'elephant', b'maxwell'])")
flags.DEFINE_integer("statistics_interval", 5,
                     "Print statistics every n seconds.")
flags.DEFINE_integer("summary_interval", 5,
                     "Save training summary to file every n seconds (rounded "
                     "up to statistics interval).")
flags.DEFINE_integer("checkpoint_interval", 600,
                     "Checkpoint the model (i.e. save the parameters) every n "
                     "seconds (rounded up to statistics interval).")

FLAGS = flags.FLAGS


class Options(object):
  """Options used by our word2vec model."""

  def __init__(self):
    # Model options.

    # Embedding dimension.
    self.emb_dim = FLAGS.embedding_size

    # Training options.
    # The training text file.
    self.train_data = FLAGS.train_data

    # Number of negative samples per example.
    self.num_samples = FLAGS.num_neg_samples

    # The initial learning rate.
    self.learning_rate = FLAGS.learning_rate

    # Number of epochs to train. After these many epochs, the learning
    # rate decays linearly to zero and the training stops.
    self.epochs_to_train = FLAGS.epochs_to_train

    # Concurrent training steps.
    self.concurrent_steps = FLAGS.concurrent_steps

    # Number of examples for one training step.
    self.batch_size = FLAGS.batch_size

    # The number of words to predict to the left and right of the target word.
    self.window_size = FLAGS.window_size

    # The minimum number of word occurrences for it to be included in the
    # vocabulary.
    self.min_count = FLAGS.min_count

    # Subsampling threshold for word occurrence.
    self.subsample = FLAGS.subsample

    # How often to print statistics.
    self.statistics_interval = FLAGS.statistics_interval

    # How often to write to the summary file (rounds up to the nearest
    # statistics_interval).
    self.summary_interval = FLAGS.summary_interval

    # How often to write checkpoints (rounds up to the nearest statistics
    # interval).
    self.checkpoint_interval = FLAGS.checkpoint_interval

    # Where to write out summaries.
    self.save_path = FLAGS.save_path
    if not os.path.exists(self.save_path):
      os.makedirs(self.save_path)

    # Eval options.
    # The text file for eval.
    self.eval_data = FLAGS.eval_data

    self.num_hash_func = FLAGS.num_hash_func
    self.hash_func_max = FLAGS.hash_func_max
    self.plk_table = FLAGS.plk_table
    self.restore_model = FLAGS.restore_model


class Word2Vec(object):
  """Word2Vec model (Skipgram)."""

  def __init__(self, options, session, should_sort, bf):
    self._options = options
    self._session = session
    self._word2id = {}
    self._id2word = []
    self.build_graph(should_sort)
    self.build_eval_graph()
    self._bf = bf

  def read_analogies(self):
    """Reads through the analogy question file.

    Returns:
      questions: a [n, 4] numpy array containing the analogy question's
                 word ids.
      questions_skipped: questions skipped due to unknown words.
    """
    questions = []
    questions_skipped = 0
    with open(self._options.eval_data, "rb") as analogy_f:
      for line in analogy_f:
        if line.startswith(b":"):  # Skip comments.
          continue
        words = line.strip().lower().split(b" ")
        ids = [self._word2id.get(w.strip()) for w in words]
        if None in ids or len(ids) != 4:
          questions_skipped += 1
        else:
          questions.append(np.array(ids))
    print("Eval analogy file: ", self._options.eval_data)
    print("Questions: ", len(questions))
    print("Skipped: ", questions_skipped)
    self._analogy_questions = np.array(questions, dtype=np.int32)


  def forward(self, examples, labels):
    """Build the graph for the forward pass."""
    opts = self._options

    # Declare all variables we need.
    # Embedding: [vocab_size, emb_dim]
    init_width = 0.5 / opts.emb_dim
    emb = tf.Variable(
        tf.random_uniform(
            [opts.hash_func_max, opts.emb_dim], -init_width, init_width),
        name="emb")
    self._emb = emb

    # Softmax weight: [vocab_size, emb_dim]. Transposed.
    sm_w_t = tf.Variable(
        tf.zeros([opts.hash_func_max, opts.emb_dim]),
        name="sm_w_t")

    # Softmax bias: [vocab_size].
    sm_b = tf.Variable(tf.zeros([opts.hash_func_max]), name="sm_b")

    # Global step: scalar, i.e., shape [].
    self.global_step = tf.Variable(0, name="global_step")

    # Nodes to compute the nce loss w/ candidate sampling.
    labels_matrix = tf.reshape(
        tf.cast(labels,
                dtype=tf.int64),
        [opts.batch_size, 1])


    example_indices = tf.nn.embedding_lookup(self._id2word, examples)
    example_emb = self.get_bf_embs(example_indices)
    
    logits = tf.matmul(example_emb, sm_w_t, transpose_b=True) + sm_b
    
    # The labels is a list of int
    labels_indices = tf.nn.embedding_lookup(self._id2word, labels)
    labels_ground_truth = tf.clip_by_value(
                            tf.reduce_sum(tf.one_hot(labels_indices, opts.hash_func_max), 1),
                            0, 1)

    return logits, labels_ground_truth


  def optimize(self, loss):
    """Build the graph to optimize the loss function."""

    # Optimizer nodes.
    # Linear learning rate decay.
    opts = self._options
    words_to_train = float(opts.words_per_epoch * opts.epochs_to_train)
    lr = opts.learning_rate * tf.maximum(
        0.0001, 1.0 - tf.cast(self._words, tf.float32) / words_to_train)
    self._lr = lr
    optimizer = tf.train.GradientDescentOptimizer(lr)
    train = optimizer.minimize(loss,
                               global_step=self.global_step,
                               gate_gradients=optimizer.GATE_NONE)
    self._train = train


  def get_bf_embs(self, all_indices):
    emb = tf.nn.embedding_lookup(self._emb, all_indices)
    emb = tf.reduce_mean(emb, 1)
    return emb

  def build_eval_graph(self):
    """Build the eval graph."""
    # Eval graph

    # Each analogy task is to predict the 4th word (d) given three
    # words: a, b, c.  E.g., a=italy, b=rome, c=france, we should
    # predict d=paris.

    # The eval feeds three vectors of word ids for a, b, c, each of
    # which is of size N, where N is the number of analogies we want to
    # evaluate in one batch.
    analogy_a = tf.placeholder(dtype=tf.int32)  # [num_hash_func]
    analogy_b = tf.placeholder(dtype=tf.int32)  # [num_hash_func]
    analogy_c = tf.placeholder(dtype=tf.int32)  # [num_hash_func]

    # Normalized word embeddings of shape [vocab_size, emb_dim].
    nemb = tf.nn.l2_normalize(self._emb, 1)

    # Each row of a_emb, b_emb, c_emb is a word's embedding vector.
    # They all have the shape [N, emb_dim]
    a_emb = self.get_bf_embs([analogy_a])  # a's embs
    b_emb = self.get_bf_embs([analogy_b])  # b's embs
    c_emb = self.get_bf_embs([analogy_c])  # c's embs

    # We expect that d's embedding vectors on the unit hyper-sphere is
    # near: c_emb + (b_emb - a_emb), which has the shape [N, emb_dim].
    target = c_emb + (b_emb - a_emb)
    norm = tf.sqrt(tf.reduce_sum(tf.square(target), 1, keep_dims=True))
    target = target / norm

    # Nodes for computing neighbors for a given word according to
    # their cosine distance.
    nearby_word = tf.placeholder(dtype=tf.int32)
    nearby_emb = self.get_bf_embs(nearby_word)
    norm = tf.sqrt(tf.reduce_sum(tf.square(nearby_emb), 1, keep_dims=True))
    nearby_emb = nearby_emb / norm

    all_words_emb = self.get_bf_embs(self._id2word)
    norm = tf.sqrt(tf.reduce_sum(tf.square(all_words_emb), 1, keep_dims=True))
    all_words_emb = all_words_emb / norm
    
    nearby_dist = tf.matmul(nearby_emb, all_words_emb, transpose_b=True)
    nearby_val, nearby_idx = tf.nn.top_k(nearby_dist,
                                         min(1000, self._options.vocab_size))


    # Compute cosine distance between each pair of target and vocab.
    dist = tf.matmul(target, all_words_emb, transpose_b=True)
    _, pred_idx = tf.nn.top_k(dist, 4)

    # Nodes in the construct graph which are used by training and
    # evaluation to run/feed/fetch.
    self._analogy_a = analogy_a
    self._analogy_b = analogy_b
    self._analogy_c = analogy_c
    self._analogy_pred_idx = pred_idx
    self._nearby_word = nearby_word
    self._nearby_val = nearby_val
    self._nearby_idx = nearby_idx


  def build_graph(self, should_sort):
    """Build the graph for the full model."""
    opts = self._options
    # The training data. A text file.
    (words, counts, words_per_epoch, self._epoch, self._words, examples,
     labels) = word2vec.skipgram_word2vec(filename=opts.train_data,
                                          batch_size=opts.batch_size,
                                          window_size=opts.window_size,
                                          min_count=opts.min_count,
                                          subsample=opts.subsample,
                                          should_sort=should_sort,
                                          num_hash_func=opts.num_hash_func)
    (opts.vocab_words, opts.vocab_counts,
     opts.words_per_epoch) = self._session.run([words, counts, words_per_epoch])
    opts.vocab_size = len(opts.vocab_words)
    print("Data file: ", opts.train_data)
    print("Vocab size: ", opts.vocab_size - 1, " + UNK")
    print("Words per epoch: ", opts.words_per_epoch)
    self._examples = examples
    self._labels = labels
    self._id2word = opts.vocab_words
    for i, w in enumerate(self._id2word):
      self._word2id[tuple(w)] = i
    logits, labels = self.forward(examples, labels)
    
    # cross-entropy(logits, labels)
    ent = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=labels, logits=logits)

    loss = tf.reduce_sum(ent) / opts.batch_size

    tf.summary.scalar("Loss", loss)
    self._loss = loss
    self.optimize(loss)

    # Properly initialize all variables.
    tf.global_variables_initializer().run()

    self.saver = tf.train.Saver()

  def save_vocab(self):
    """Save the vocabulary to a file so the model can be reloaded."""
    opts = self._options
    with open(os.path.join(opts.save_path, "vocab.txt"), "w") as f:
      for i in xrange(opts.vocab_size):
        vocab_word = tf.compat.as_text(opts.vocab_words[i]).encode("utf-8")
        f.write("%s %d\n" % (vocab_word,
                             opts.vocab_counts[i]))

  def _train_thread_body(self):
    initial_epoch, = self._session.run([self._epoch])
    while True:
      _, epoch = self._session.run([self._train, self._epoch])
      if epoch != initial_epoch:
        break

  def train(self):
    """Train the model."""
    opts = self._options

    initial_epoch, initial_words = self._session.run([self._epoch, self._words])

    summary_op = tf.summary.merge_all()
    summary_writer = tf.summary.FileWriter(opts.save_path, self._session.graph)
    workers = []
    for _ in xrange(opts.concurrent_steps):
      t = threading.Thread(target=self._train_thread_body)
      t.start()
      workers.append(t)

    last_words, last_time, last_summary_time = initial_words, time.time(), 0
    last_checkpoint_time = 0
    while True:
      time.sleep(opts.statistics_interval)  # Reports our progress once a while.
      (epoch, step, loss, words, lr) = self._session.run(
          [self._epoch, self.global_step, self._loss, self._words, self._lr])
      now = time.time()
      last_words, last_time, rate = words, now, (words - last_words) / (
          now - last_time)
      print("Epoch %4d Step %8d: lr = %5.3f loss = %6.2f words/sec = %8.0f\r" %
            (epoch, step, lr, loss, rate), end="")
      sys.stdout.flush()
      if now - last_summary_time > opts.summary_interval:
        summary_str = self._session.run(summary_op)
        summary_writer.add_summary(summary_str, step)
        last_summary_time = now
      if now - last_checkpoint_time > opts.checkpoint_interval:
        self.saver.save(self._session,
                        os.path.join(opts.save_path, "model.ckpt"),
                        global_step=step.astype(int))
        last_checkpoint_time = now
      if epoch != initial_epoch:
        break

    for t in workers:
      t.join()

    return epoch

  def _predict(self, analogy):
    """Predict the top 4 answers for analogy questions."""
    idx, = self._session.run([self._analogy_pred_idx], {
        self._analogy_a: analogy[0],
        self._analogy_b: analogy[1],
        self._analogy_c: analogy[2]
    })
    return idx

  def eval(self):
    """Evaluate analogy questions and reports accuracy."""

    # How many questions we get right at precision@1.
    correct = 0

    try:
      total = self._analogy_questions.shape[0]
    except AttributeError as e:
      raise AttributeError("Need to read analogy questions.")

    start = 0
    while start < total:
      limit = start + 2500
      sub = self._analogy_questions[start:limit, :]
      idx = self._predict(sub)
      start = limit
      for question in xrange(sub.shape[0]):
        for j in xrange(4):
          if idx[question, j] == sub[question, 3]:
            # Bingo! We predicted correctly. E.g., [italy, rome, france, paris].
            correct += 1
            break
          elif idx[question, j] in sub[question, :3]:
            # We need to skip words already in the question.
            continue
          else:
            # The correct label is not the precision@1
            break
    print()
    print("Eval %4d/%d accuracy = %4.1f%%" % (correct, total,
                                              correct * 100.0 / total))


  def analogy(self, w0, w1, w2):
    """Predict word w3 as in w0:w1 vs w2:w3."""
    wid = np.array([self._bf.get_indices(w) for w in [w0, w1, w2]])
    idx = self._predict(wid)
    found = False
    for c in [self._id2word[i] for i in idx[0, :]]:
      possible_words = self._bf.get_possible_words_by_indices(c)
      possible_word = next(iter(possible_words))
      if possible_word not in [w0, w1, w2]:
        print(possible_word)
        found = True
        break
    if not found:
      print('Cannot find analogy with [{}, {}, {}].'.format(w0, w1, w2))


  def nearby(self, words, num=20):
    """Prints out nearby words given a list of words."""

    tmp = words
    words = []
    for word in tmp:
      words.append(self._bf.get_indices(word))
    
    vals, idx = self._session.run(
        [self._nearby_val, self._nearby_idx], {self._nearby_word: words})
    for i in xrange(len(words)):
      print("\n%s\n=====================================" % (words[i]))
      for (neighbor, distance) in zip(idx[i, :num], vals[i, :num]):
        neighbor_indices = self._id2word[neighbor]

        possible_words = self._bf.get_possible_words_by_indices(neighbor_indices)
        if len(possible_words) == 0:
            print('Unable to find reversed word for: {}'.format(neighbor_indices))
        else:
            print('{:>2.6f}\t{}'.format(distance, possible_words))


def _start_shell(local_ns=None):
  # An interactive shell is useful for debugging/development.
  import IPython
  user_ns = {}
  if local_ns:
    user_ns.update(local_ns)
  user_ns.update(globals())
  IPython.start_ipython(argv=[], user_ns=user_ns)


def main(_):
  """Train a word2vec model."""
  if not FLAGS.train_data or not FLAGS.save_path:
    print("--train_data and --save_path must be specified.")
    sys.exit(1)
  opts = Options()
  bf = None
  with tf.Graph().as_default(), tf.Session() as session:
    if opts.plk_table:
      bf = bloomfilter()
      bf.load(opts.plk_table)

    if FLAGS.interactive and opts.plk_table and opts.restore_model:
      with tf.device("/gpu:1"):
        model = Word2Vec(opts, session, False, bf)
        model.saver.restore(session, tf.train.latest_checkpoint(opts.save_path))

      _start_shell(locals())
    else:
      with tf.device("/gpu:1"):
        model = Word2Vec(opts, session, True, bf)
      if opts.restore_model:
        model.saver.restore(session, tf.train.latest_checkpoint(opts.save_path))
      for _ in xrange(opts.epochs_to_train):
        model.train()  # Process one epoch
        # model.eval()  # Eval analogies.
      # Perform a final save.
      model.saver.save(session,
                      os.path.join(opts.save_path, "model.ckpt"),
                      global_step=model.global_step)

if __name__ == "__main__":
  tf.app.run()
