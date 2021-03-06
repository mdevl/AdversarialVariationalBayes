import tensorflow as tf
import numpy as np
import scipy as sp
import time
from tqdm import tqdm
from avb.decoders import get_reconstr_err

class AIS(object):
    def __init__(self, decoder, config):
        self.decoder = decoder
        self.config = config

        self.build_model()

    def build_model(self):
        batch_size = self.config['batch_size']
        z_dim = self.config['z_dim']
        output_size = self.config['output_size']
        c_dim = self.config['c_dim']
        # Input
        self.x_in = tf.placeholder(tf.float32)
        self.mean0_in = tf.placeholder(tf.float32)
        self.std0_in = tf.placeholder(tf.float32)

        # Persist on gpu for efficiency
        self.x = tf.Variable(np.zeros([batch_size, output_size, output_size, c_dim], dtype=np.float32), trainable=False)
        self.mean0 = tf.Variable(np.zeros([batch_size, z_dim], dtype=np.float32), trainable=False)
        self.std0 = tf.Variable(np.ones([batch_size, z_dim], dtype=np.float32), trainable=False)
        self.var0 = tf.square(self.std0)

        # Position and momentum variables
        mass = 1.#/self.var0
        mass_sqrt = 1.#/self.std0
        self.z = tf.Variable(np.zeros([batch_size, z_dim], dtype=np.float32), trainable=False)
        self.p = tf.Variable(np.zeros([batch_size, z_dim], dtype=np.float32), trainable=False)

        self.z_current = tf.Variable(np.zeros([batch_size, z_dim], dtype=np.float32), trainable=False)
        self.p_current = tf.Variable(np.zeros([batch_size, z_dim], dtype=np.float32), trainable=False)

        self.p_rnd = tf.random_normal([batch_size, z_dim]) * mass_sqrt

        self.eps = tf.placeholder(tf.float32, shape=[])
        self.beta = tf.placeholder(tf.float32, shape=[])

        # Hamiltoninan

        self.U = self.get_energy(self.z)
        self.V = 0.5 * tf.reduce_sum(tf.square(self.p)/mass, [1])
        self.H = self.U + self.V
        self.U_current = self.get_energy(self.z_current)
        self.V_current = 0.5 * tf.reduce_sum(tf.square(self.p_current)/mass, [1])
        self.H_current = self.U_current + self.V_current

        # Intialize
        self.init_hmc = [
            self.x.assign(self.x_in),
            self.mean0.assign(self.mean0_in),
            self.std0.assign(self.std0_in)
        ]

        self.init_z = self.z_current.assign(
            self.mean0 + self.std0 * tf.random_normal([batch_size, z_dim])
        )

        self.init_hmc_step = [
            # self.z.assign(self.z_current),
            # self.p.assign(self.p_rnd),
            self.p_current.assign(self.p_rnd)
        ]

        self.init_hmc_step2 = [
            self.z.assign(self.z_current),
            self.p.assign(self.p_current),
        ]
        # Euler steps
        eps_scaled = self.std0 * self.eps

        self.euler_z = self.z.assign_add(eps_scaled * self.p/mass)
        gradU = tf.reshape(tf.gradients(self.U, self.z), [batch_size, z_dim])
        self.euler_p = self.p.assign_sub(eps_scaled * gradU)

        # Accept
        self.is_accept = tf.cast(tf.random_uniform([batch_size]) < tf.exp(self.H_current - self.H), tf.float32)
        self.accept_rate = tf.reduce_mean(self.is_accept)

        is_accept_rs = tf.reshape(self.is_accept, [batch_size, 1])
        self.update_z = self.z_current.assign(
            is_accept_rs * self.z + (1. - is_accept_rs) * self.z_current
        )

    def get_energy(self, z):
        E = self.beta*self.get_energy1(z) + (1 - self.beta) * self.get_energy0(z)
        return E

    def get_energy1(self, z):
        decoder_out = self.decoder(z)
        E = get_reconstr_err(decoder_out, self.x, self.config)
        # Prior
        E += tf.reduce_sum(
            0.5 * tf.square(z) + 0.5 * np.log(2*np.pi), [1]
        )

        return E

    def get_energy0(self, z):
        z_norm = (z - self.mean0) / self.std0
        E = tf.reduce_sum(
            0.5 * z_norm * z_norm + tf.log(self.std0) + 0.5 * np.log(2*np.pi), [1]
        )
        return E

    def evaluate(self, sess, x_test, mean0=None, std0=None):
        is_adaptive_eps = self.config['test_is_adaptive_eps']
        nsteps = self.config['test_ais_nsteps']
        batch_size = self.config['batch_size']
        eps = self.config['test_ais_eps']

        if mean0 is None:
            mean0 = np.zeros([self.batch_size, self.z_dim], dtype=np.float32)
        if std0 is None:
            std0 = np.ones([self.batch_size, self.z_dim], dtype=np.float32)

        # logZ = sess.run(tf.reduce_sum(tf.log(self.std0), [1]))
        # logZ = self.z_dim * 0.5 * np.log(2*np.pi)
        logpx = 0.
        weights = np.zeros([100, batch_size])

        betas = np.linspace(0, 1, nsteps+1)
        accept_rate = 1.

        feed_dict = {
            self.x_in: x_test,
            self.mean0_in: mean0, self.std0_in: std0
        }

        sess.run(self.init_hmc, feed_dict=feed_dict)
        sess.run(self.init_z)

        t = time.time()
        progress = tqdm(range(nsteps), desc="HMC")
        for i in progress:
            f0 = -sess.run(self.U_current, feed_dict={self.beta: betas[i]})
            f1 = -sess.run(self.U_current, feed_dict={self.beta: betas[i+1]})
            logpx += f1 - f0

            if i < nsteps-1:
                accept_rate = self.run_hmc_step(sess, betas[i+1], eps)
                if is_adaptive_eps and accept_rate < 0.6:
                    eps = eps / 1.1
                elif is_adaptive_eps and accept_rate > 0.7:
                    eps = eps * 1.1
                progress.set_postfix(
                    accept_rate="%.2f" % accept_rate,
                    logw="%.2f+-%.2f" % (logpx.mean(), logpx.std()),
                    eps="%.2e" % eps,
                )
        samples = sess.run(self.z_current)

        return logpx, samples

    def run_hmc_step(self, sess, beta, eps):
        L = 10 # TODO: make configuratble
        # Initialize
        sess.run(self.init_hmc_step)
        sess.run(self.init_hmc_step2)

        # Leapfrog steps
        sess.run(self.euler_p, feed_dict={self.eps: eps/2, self.beta: beta})
        for i in range(L+1):
            sess.run(self.euler_z, feed_dict={self.eps: eps, self.beta: beta})
            if i < L:
                sess.run(self.euler_p, feed_dict={self.eps: eps, self.beta: beta})
        sess.run(self.euler_p, feed_dict={self.eps: eps/2, self.beta: beta})

        # Update z
        _, accept_rate = sess.run([self.update_z, self.accept_rate], feed_dict={self.beta: beta})
        return accept_rate


    def average_weights(self, weights, axis=0):
        nchains = weights.shape[axis]
        logsumw = sp.misc.logsumexp(weights, axis=axis)
        lprob = logsumw - np.log(nchains)
        ess = np.exp(-sp.misc.logsumexp(2*(weights - logsumw), axis=axis))
        return lprob, ess
