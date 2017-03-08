from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()
import argparse

import chainer
from chainer import functions as F
from chainer import links as L
import gym
import gym.wrappers
import numpy as np

import chainerrl
from chainerrl.agents import a3c
from chainerrl import experiments
from chainerrl import links
from chainerrl import misc
from chainerrl.optimizers.nonbias_weight_decay import NonbiasWeightDecay
from chainerrl.optimizers import rmsprop_async
from chainerrl import policies
from chainerrl.recurrent import RecurrentChainMixin
from chainerrl import v_function


def phi(obs):
    return obs.astype(np.float32)


class A3CFFSoftmax(chainer.ChainList, a3c.A3CModel):

    def __init__(self, ndim_obs, n_actions, hidden_sizes=(50, 50)):
        self.pi = policies.SoftmaxPolicy(
            model=links.MLP(ndim_obs, n_actions, hidden_sizes))
        self.v = links.MLP(ndim_obs, 1, hidden_sizes=hidden_sizes)
        super().__init__(self.pi, self.v)

    def pi_and_v(self, state):
        return self.pi(state), self.v(state)


class A3CFFMellowmax(chainer.ChainList, a3c.A3CModel):

    def __init__(self, ndim_obs, n_actions, hidden_sizes=(200, 200)):
        self.pi = policies.MellowmaxPolicy(
            model=links.MLP(ndim_obs, n_actions, hidden_sizes))
        self.v = links.MLP(ndim_obs, 1, hidden_sizes=hidden_sizes)
        super().__init__(self.pi, self.v)

    def pi_and_v(self, state):
        return self.pi(state), self.v(state)


class A3CLSTMGaussian(chainer.ChainList, a3c.A3CModel, RecurrentChainMixin):

    def __init__(self, obs_size, action_size, hidden_size=50, lstm_size=50):
        self.pi_head = L.Linear(obs_size, hidden_size)
        self.v_head = L.Linear(obs_size, hidden_size)
        self.pi_lstm = L.LSTM(hidden_size, lstm_size)
        self.v_lstm = L.LSTM(hidden_size, lstm_size)
        self.pi = policies.LinearGaussianPolicyWithDiagonalCovariance(
            lstm_size, action_size)
        self.v = v_function.FCVFunction(lstm_size)
        super().__init__(self.pi_head, self.v_head,
                         self.pi_lstm, self.v_lstm, self.pi, self.v)

    def pi_and_v(self, state):

        def forward(head, lstm, tail):
            h = F.relu(head(state))
            # h = lstm(h)
            return tail(h)

        pout = forward(self.pi_head, self.pi_lstm, self.pi)
        vout = forward(self.v_head, self.v_lstm, self.v)

        return pout, vout


def main():
    import logging

    parser = argparse.ArgumentParser()
    parser.add_argument('--processes', type=int, default=8)
    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--env', type=str, default='CartPole-v0')
    parser.add_argument('--arch', type=str, default='FFSoftmax',
                        choices=('FFSoftmax', 'FFMellowmax', 'LSTMGaussian'))
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--outdir', type=str, default=None)
    parser.add_argument('--batchsize', type=int, default=10)
    parser.add_argument('--rollout-len', type=int, default=10)
    parser.add_argument('--n-hidden-channels', type=int, default=100)
    parser.add_argument('--n-hidden-layers', type=int, default=2)
    parser.add_argument('--n-times-replay', type=int, default=1)
    parser.add_argument('--replay-start-size', type=int, default=2000)
    parser.add_argument('--t-max', type=int, default=5)
    parser.add_argument('--tau', type=float, default=1e-2)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--steps', type=int, default=8 * 10 ** 7)
    parser.add_argument('--eval-frequency', type=int, default=10 ** 5)
    parser.add_argument('--eval-n-runs', type=int, default=10)
    parser.add_argument('--reward-scale-factor', type=float, default=1e-2)
    parser.add_argument('--rmsprop-epsilon', type=float, default=1e-1)
    parser.add_argument('--render', action='store_true', default=False)
    parser.add_argument('--lr', type=float, default=7e-4)
    parser.add_argument('--weight-decay', type=float, default=0.0)
    parser.add_argument('--demo', action='store_true', default=False)
    parser.add_argument('--load', type=str, default='')
    parser.add_argument('--logger-level', type=int, default=logging.DEBUG)
    parser.add_argument('--monitor', action='store_true')
    parser.add_argument('--train-async', action='store_true', default=False)
    parser.add_argument('--disable-online-update', action='store_true',
                        default=False)
    parser.add_argument('--backprop-future-values', action='store_true',
                        default=False)
    args = parser.parse_args()

    logging.getLogger().setLevel(args.logger_level)

    if args.seed is not None:
        misc.set_random_seed(args.seed)

    args.outdir = experiments.prepare_output_dir(args, args.outdir)

    def make_env(process_idx, test):
        env = gym.make(args.env)
        if args.monitor and process_idx == 0:
            env = gym.wrappers.Monitor(env, args.outdir)
        # Scale rewards observed by agents
        if not test:
            misc.env_modifiers.make_reward_filtered(
                env, lambda x: x * args.reward_scale_factor)
        if args.render and process_idx == 0 and not test:
            misc.env_modifiers.make_rendered(env)
        return env

    sample_env = gym.make(args.env)
    timestep_limit = sample_env.spec.tags.get(
        'wrapper_config.TimeLimit.max_episode_steps')
    obs_space = sample_env.observation_space
    action_space = sample_env.action_space

    # Switch policy types accordingly to action space types
    if isinstance(action_space, gym.spaces.Box):
        # model = A3CLSTMGaussian(obs_space.low.size, action_space.low.size)
        model = a3c.A3CSeparateModel(
            pi=chainerrl.policies.FCGaussianPolicy(
                obs_space.low.size, action_space.low.size,
                n_hidden_channels=args.n_hidden_channels,
                n_hidden_layers=args.n_hidden_layers,
                bound_mean=True,
                min_action=action_space.low,
                max_action=action_space.high,
                var_wscale=1e-3,
                var_bias=1,
                var_type='diagonal',
                # nonlinearity=F.elu,
                # min_var=1e-2,
            ),
            v=chainerrl.v_functions.FCVFunction(
                obs_space.low.size,
                n_hidden_channels=args.n_hidden_channels,
                n_hidden_layers=args.n_hidden_layers,
                # nonlinearity=F.elu,
            )
        )
    else:
        model = A3CFFSoftmax(obs_space.low.size, action_space.n)
    if not args.train_async and args.gpu >= 0:
        model.to_gpu(args.gpu)

    opt = rmsprop_async.RMSpropAsync(
        lr=args.lr, eps=args.rmsprop_epsilon, alpha=0.99)
    opt.setup(model)
    opt.add_hook(chainer.optimizer.GradientClipping(40))
    if args.weight_decay > 0:
        opt.add_hook(NonbiasWeightDecay(args.weight_decay))

    # agent = a3c.A3C(model, opt, t_max=args.t_max, gamma=0.99,
    #                 beta=args.beta, phi=phi)
    replay_buffer = chainerrl.replay_buffer.EpisodicReplayBuffer(10 ** 5)
    # explorer = chainerrl.explorers.AdditiveOU(start_with_mu=True)
    explorer = None
    agent = chainerrl.agents.PCL(
        model, opt, replay_buffer=replay_buffer,
        t_max=args.t_max, gamma=0.99,
        tau=args.tau,
        phi=phi,
        rollout_len=args.rollout_len,
        n_times_replay=args.n_times_replay,
        replay_start_size=args.replay_start_size,
        batchsize=args.batchsize,
        explorer=explorer,
        train_async=args.train_async,
        disable_online_update=args.disable_online_update,
        backprop_future_values=args.backprop_future_values,
    )
    if args.load:
        agent.load(args.load)

    if args.demo:
        env = make_env(0, True)
        mean, median, stdev = experiments.eval_performance(
            env=env,
            agent=agent,
            n_runs=args.eval_n_runs,
            max_episode_len=timestep_limit)
        print('n_runs: {} mean: {} median: {} stdev'.format(
            args.eval_n_runs, mean, median, stdev))
    else:
        if args.train_async:
            experiments.train_agent_async(
                agent=agent,
                outdir=args.outdir,
                processes=args.processes,
                make_env=make_env,
                profile=args.profile,
                steps=args.steps,
                eval_n_runs=args.eval_n_runs,
                eval_frequency=args.eval_frequency,
                max_episode_len=timestep_limit)
        else:
            experiments.train_agent_with_evaluation(
                agent=agent,
                env=make_env(0, test=False),
                eval_env=make_env(0, test=True),
                outdir=args.outdir,
                steps=args.steps,
                eval_n_runs=args.eval_n_runs,
                eval_frequency=args.eval_frequency,
                max_episode_len=timestep_limit)


if __name__ == '__main__':
    main()
