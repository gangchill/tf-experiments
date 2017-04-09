from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import sys
import os
import math
import time
import collections

import functools
import operator

import attr

import gym

import tensorflow as tf
import numpy as np
import scipy.signal

WIDTH  = 210
HEIGHT = 160
PLANES = 1
ACTIONS = 2

DISCOUNT = 0.99

FLAGS = None

def normalized_columns_initializer(std=1.0):
    def _initializer(shape, dtype=None, partition_info=None):
        out = np.random.randn(*shape).astype(np.float32)
        out *= std / np.sqrt(np.square(out).sum(axis=0, keepdims=True))
        return tf.constant(out)
    return _initializer

class PingPongModel(object):
  def __init__(self):
    with tf.variable_scope('Frames'):
      self.prev_frame = tf.placeholder(tf.float32, [None, WIDTH, HEIGHT, PLANES], name="ThisFrame")
      self.this_frame = tf.placeholder(tf.float32, [None, WIDTH, HEIGHT, PLANES], name="PrevFrame")

    deltas = self.this_frame - self.prev_frame
    deltas = deltas[:,::2,::2]

    frame = tf.reshape(deltas, (-1, WIDTH//2, HEIGHT//2, PLANES))

    activations = tf.contrib.layers.conv2d(
      frame,
      biases_initializer = tf.constant_initializer(0.1),
      num_outputs=16,
      padding='SAME',
      kernel_size=4,
      trainable=True,
      stride=2,
      activation_fn=tf.nn.relu,
      scope='Conv_1',
    )
    activations = tf.contrib.layers.max_pool2d(
      activations,
      kernel_size=2,
    )
    activations = tf.contrib.layers.conv2d(
      activations,
      biases_initializer = tf.constant_initializer(0.1),
      num_outputs=16,
      padding='SAME',
      kernel_size=4,
      trainable=True,
      stride=2,
      activation_fn=tf.nn.relu,
      scope='Conv_2',
    )
    activations = tf.contrib.layers.max_pool2d(
      activations,
      kernel_size=2,
    )

    self.activations = activations

    z_h = tf.contrib.layers.fully_connected(
      tf.contrib.layers.flatten(activations),
      num_outputs = FLAGS.hidden,
      activation_fn = tf.nn.relu,
      biases_initializer = tf.constant_initializer(0),
      trainable = True,
      scope='Hidden',
    )

    self.logits = tf.contrib.layers.fully_connected(
      z_h,
      num_outputs = ACTIONS,
      weights_initializer = normalized_columns_initializer(0.01),
      biases_initializer = tf.constant_initializer(0),
      trainable = True,
      scope = 'Actions',
      activation_fn=None,
    )

    self.vp = tf.reshape(tf.contrib.layers.fully_connected(
      z_h,
      num_outputs = 1,
      biases_initializer = tf.constant_initializer(-1),
      trainable = True,
      scope = 'Values',
      activation_fn=tf.tanh,
    ), (-1,))

    tf.summary.histogram('logits', self.logits)
    self.act_probs = tf.nn.softmax(self.logits)

  def add_train_ops(self):
    with tf.variable_scope('Train'):
      self.adv  = tf.placeholder(tf.float32, [None], name="Advantage")
      tf.summary.histogram('advantage', self.adv)
      self.rewards = tf.placeholder(tf.float32, [None], name="Reward")
      tf.summary.histogram('weighted_reward', self.rewards)
      self.actions = tf.placeholder(tf.float32, [None, ACTIONS], name="SampledActions")

      self.cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=self.actions, logits=self.logits)
      self.pg_loss = tf.reduce_mean(self.adv * self.cross_entropy)
      tf.summary.scalar('pg_loss', self.pg_loss)
      self.v_loss = 0.5 * tf.reduce_mean(tf.square(self.vp - self.rewards))
      tf.summary.scalar('value_loss', self.v_loss)
      tf.summary.histogram('value_err', (self.vp - self.rewards))
      self.entropy = -tf.reduce_mean(
        tf.reduce_sum(self.act_probs * tf.nn.log_softmax(self.logits), axis=1))
      tf.summary.scalar('entropy', self.entropy)

      self.loss = (
        FLAGS.pg_weight * self.pg_loss +
        FLAGS.v_weight * self.v_loss -
        FLAGS.entropy_weight * self.entropy)

      self.optimizer = tf.train.AdamOptimizer(FLAGS.eta)
      grads = self.optimizer.compute_gradients(self.loss)
      clipped, norm = tf.clip_by_global_norm(
        [g for (g, v) in grads], 40.0)
      tf.summary.scalar('grad_norm', norm)
      self.train_step = self.optimizer.apply_gradients(
        (c, v) for (c, (_,v)) in zip(clipped, grads))
    for var in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES):
      tf.summary.scalar('norm/' + var.name, tf.norm(var))

def discount(x, gamma):
    return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

@attr.s
class Rollout(object):
  frames    = attr.ib(default=attr.Factory(list))
  actions   = attr.ib(default=attr.Factory(list))
  rewards   = attr.ib(default=attr.Factory(list))
  vp        = attr.ib(default=attr.Factory(list))

  discounted = attr.ib(default=None)
  start_time = attr.ib(default=None)
  end_time   = attr.ib(default=None)

  def clear(self):
    del self.frames[:]
    del self.actions[:]
    del self.rewards[:]
    del self.vp[:]
    self.start_time = time.time()

def build_rewards(rollout):
  discounted = np.zeros((len(rollout.actions),))
  r = 0
  for i,rw in reversed(list(enumerate(rollout.rewards))):
    if rw != 0:
      r = rw
    discounted[i] = r
    r *= DISCOUNT

  rollout.discounted = discounted
  return rollout.discounted

def build_advantage(rollout):
  return rollout.discounted - rollout.vp

def build_actions(rollout):
  actions = np.zeros((len(rollout.actions), ACTIONS))
  actions[np.arange(len(actions)), rollout.actions] = 1
  return actions

def process_frame(frame):
  return np.expand_dims(np.mean(frame, 2), -1)

def generate_rollouts(session, model):
  env = gym.make('Pong-v0')
  this_frame = process_frame(env.reset())
  prev_frame = np.zeros_like(this_frame)

  rollout = Rollout(
    frames=[prev_frame],
    start_time=time.time(),
  )
  while True:
    if FLAGS.render:
      env.render()

    act_probs, vp = session.run(
      [model.act_probs, model.vp],
      feed_dict=
      {
        model.this_frame: np.expand_dims(this_frame, 0),
        model.prev_frame: np.expand_dims(prev_frame, 0)
      })
    r = np.random.uniform()
    for i, a in enumerate(act_probs[0]):
      if r <= a:
        action = i
        break
      r -= a
    # action = 2 if np.random.uniform() < 0.5 else 3

    next_frame, reward, done, info = env.step(2 + action)

    prev_frame = this_frame
    this_frame = process_frame(next_frame)

    rollout.frames.append(this_frame)
    rollout.actions.append(action)
    rollout.rewards.append(reward)
    rollout.vp.append(vp[0])

    if done:
      rollout.end_time = time.time()
      yield rollout

      prev_frame = np.zeros_like(this_frame)
      rollout.clear()
      rollout.frames.append(prev_frame)

      env.reset()

class Trainer(object):
  def __init__(self, model, session, logdir):
    self.logdir = logdir
    self.global_step = tf.get_variable("global_step", [], tf.int32,
                                       initializer=tf.constant_initializer(0, dtype=tf.int32),
                                       trainable=False)
    self.inc_step = self.global_step.assign_add(1)

    self.model = model
    self.model.add_train_ops()

    self.session = session
    self.summary_op = tf.summary.merge_all()

    self.avgreward = 0

    if logdir:
      try:
        os.makedirs(os.path.dirname(logdir))
      except FileExistsError:
        pass
      self.summary_writer = tf.summary.FileWriter(logdir, session.graph)
    else:
      self.summary_op = self.summary_writer = None

    self.write_summaries = self.summary_writer and FLAGS.summary_interval
    self.write_checkpoints = logdir and FLAGS.checkpoint

  def process_rollout(self, rollout):
    train_start = time.time()

    rewards = build_rewards(rollout)
    adv     = build_advantage(rollout)
    actions = build_actions(rollout)

    ops = {
      'pg_loss': self.model.pg_loss,
      'v_loss': self.model.v_loss,
      'train': self.model.train_step,
      'step': self.inc_step,
    }
    if self.summary_op is not None:
      ops['summary'] = self.summary_op

    out = self.session.run(
      ops,
      feed_dict = {
        self.model.this_frame: rollout.frames[1:],
        self.model.prev_frame: rollout.frames[:-1],
        self.model.actions:    actions,
        self.model.rewards:    rewards,
        self.model.adv:        adv,
      })
    train_end = time.time()

    self.avgreward = 0.9 * self.avgreward + 0.1 * sum(rollout.rewards)
    print("done round={round} frames={frames} reward={reward} expreward={avgreward:.1f} pg_loss={pg_loss} v_loss={v_loss} actions={actions}".format(
      frames = len(rollout.actions),
      reward = sum(rollout.rewards),
      avgreward = self.avgreward,
      actions = collections.Counter(rollout.actions),
      pg_loss = out['pg_loss'],
      v_loss = out['v_loss'],
      round = out['step'],
    ))

    train_fps = len(rollout.actions)/(train_end-train_start)
    play_fps = len(rollout.actions)/(rollout.end_time-rollout.start_time)

    if self.write_summaries and out['step'] % FLAGS.summary_interval == 0:
      summary = tf.Summary()
      summary.value.add(tag='env/frames', simple_value=float(len(rollout.actions)))
      summary.value.add(tag='env/fps', simple_value=play_fps)
      summary.value.add(tag='train/fps', simple_value=train_fps)
      summary.value.add(tag='env/reward', simple_value=sum(rollout.rewards))
      self.summary_writer.add_summary(summary, out['step'])
      self.summary_writer.add_summary(out['summary'], out['step'])
    return out['step']


def main(_):
  model = PingPongModel()
  session = tf.InteractiveSession()

  if FLAGS.train:
    trainer = Trainer(model, session, FLAGS.logdir)

  saver = tf.train.Saver(
    max_to_keep=5, keep_checkpoint_every_n_hours=1)

  if FLAGS.load_model:
    saver.restore(session, FLAGS.load_model)
  else:
    tf.global_variables_initializer().run()


  rounds = 0

  avgreward = 0

  for rollout in generate_rollouts(session, model):
    if FLAGS.train:
      step = trainer.process_rollout(rollout)
      if trainer.write_checkpoints and out['step'] % FLAGS.checkpoint == 0:
        saver.save(session, os.path.join(out.logdir, 'pong'), global_step=out['step'])


    rounds += 1

def arg_parser():
  parser = argparse.ArgumentParser()
  parser.add_argument('--render', default=False, action='store_true',
                      help='render simulation')
  parser.add_argument('--train', default=True, action='store_true',
                      help='Train model')
  parser.add_argument('--no-train', action='store_false', dest='train',
                      help="Don't train")
  parser.add_argument('--hidden', type=int, default=256,
                      help='hidden neurons')
  parser.add_argument('--eta', type=float, default=1e-4,
                      help='learning rate')
  parser.add_argument('--checkpoint', type=int, default=0,
                      help='checkpoint every N rounds')
  parser.add_argument('--summary_interval', type=int, default=5,
                      help='write summaries every N rounds')
  parser.add_argument('--logdir', type=str, default=None,
                      help='log path')
  parser.add_argument('--load_model', type=str, default=None,
                      help='restore model')

  parser.add_argument('--debug', action='store_true',
                      help='debug spew')

  parser.add_argument('--pg_weight', type=float, default=1.0)
  parser.add_argument('--v_weight', type=float, default=0.5)
  parser.add_argument('--entropy_weight', type=float, default=0.01)
  return parser

if __name__ == '__main__':
  parser = arg_parser()
  FLAGS, unparsed = parser.parse_known_args()
  tf.app.run(main=main, argv=[sys.argv[0]] + unparsed)
else:
  FLAGS = arg_parser().parse_args([])
