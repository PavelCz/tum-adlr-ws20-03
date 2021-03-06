import torch
import torch.optim as optim
import torch.nn.functional as F

import time, os
import random
import numpy as np
from collections import deque
from time import sleep

from src.common.utils import epsilon_scheduler, print_log, save_model
from src.common.adv_train_ma_wrapper import AdvTrainWrapperNFSP
from src.selfplay.model import DQN, Policy
from src.selfplay.storage import ReplayBuffer, ReservoirBuffer
from src.selfplay.nfsp_loss import compute_sl_loss, compute_rl_loss, compute_rl_loss_DDQN

def train(env, args, writer):
    # Initialize replay memories Replay Buffer Rewervoir Buffer
    # Replay Buffer for RL - Best Response
    p1_replay_buffer = ReplayBuffer(args.buffer_size)
    p2_replay_buffer = ReplayBuffer(args.buffer_size)

    # Reservoir Buffer for SL - Average Strategy
    p1_reservoir_buffer = ReservoirBuffer(args.buffer_size)
    p2_reservoir_buffer = ReservoirBuffer(args.buffer_size)

    # Initialize Action Value Network and target network
    # RL Model for Player 1
    p1_current_model = DQN(env, args, 'p1').to(args.device)
    p1_target_model = DQN(env, args, 'p1').to(args.device)
    

    # RL Model for Player 2
    p2_current_model = DQN(env, args, 'p2').to(args.device)
    p2_target_model = DQN(env, args, 'p2').to(args.device)

    # Initialize Average Policy Network
    # SL Model for Player 1, 2
    p1_policy = Policy(env, args, 'p1').to(args.device)
    p2_policy = Policy(env, args, 'p2').to(args.device)

    epsilon_by_frame = epsilon_scheduler(args.eps_start, args.eps_final, args.eps_decay)

    # RL Optimizer for Player 1, 2
    p1_rl_optimizer = optim.Adam(p1_current_model.parameters(), lr=args.lr)
    p2_rl_optimizer = optim.Adam(p2_current_model.parameters(), lr=args.lr)

    # SL Optimizer for Player 1, 2
    p1_sl_optimizer = optim.Adam(p1_policy.parameters(), lr=args.lr)
    p2_sl_optimizer = optim.Adam(p2_policy.parameters(), lr=args.lr)

    # Deque data structure for frame skipping
    p1_state_deque = deque(maxlen=args.frame_skipping)
    p1_reward_deque = deque(maxlen=args.frame_skipping)
    p1_action_deque = deque(maxlen=args.frame_skipping)

    p2_state_deque = deque(maxlen=args.frame_skipping)
    p2_reward_deque = deque(maxlen=args.frame_skipping)
    p2_action_deque = deque(maxlen=args.frame_skipping)

    p1_target_model.load_state_dict(p1_current_model.state_dict())
    p2_target_model.load_state_dict(p2_current_model.state_dict())

    # Logging
    total_hit = 10
    p1_reward, p2_reward = 0, 0
    episode_length_list = []
    length_list = []
    p1_reward_list, p1_rl_loss_list, p1_sl_loss_list = [], [], []
    p2_reward_list, p2_rl_loss_list, p2_sl_loss_list = [], [], []
    p1_episode_reward, p2_episode_reward = 0, 0
    tag_interval_length = 0
    prev_time = time.time()
    prev_frame = 1

    # Main Loop
    state = env.reset()
    p1_state = state[0]
    p2_state = state[1]
    for frame_idx in range(1, args.max_frames + 1): 
        is_best_response = False
        # Policy is decided by a combination of Best Response and Average Strategy
        if random.random() > args.eta:
            # With probability 1 - eta choose average strategy pi
            if args.fgsm_training:
                p1_state = AdvTrainWrapperNFSP.adv_sl_ob(p1_state, p1_policy, args)
                p2_state = AdvTrainWrapperNFSP.adv_sl_ob(p2_state, p2_policy, args)
            p1_action = p1_policy.act(torch.tensor(p1_state).to(args.device))
            p2_action = p2_policy.act(torch.tensor(p2_state).to(args.device))
        else:
            # With probability eta choose best response strategy beta
            if args.fgsm_training:
                p1_state = AdvTrainWrapperNFSP.adv_rl_ob(p1_state, p1_current_model, args)
                p2_state = AdvTrainWrapperNFSP.adv_rl_ob(p2_state, p2_current_model, args)
            is_best_response = True
            epsilon = epsilon_by_frame(frame_idx)
            p1_action = p1_current_model.act(torch.tensor(p1_state).to(args.device), epsilon)
            p2_action = p2_current_model.act(torch.tensor(p2_state).to(args.device), epsilon)

        # Get actions from chosen policy
        actions = [p1_action, p2_action]
        next_state, reward, done, info = env.step(actions)

        p1_state_deque.append(p1_state)
        p2_state_deque.append(p2_state)

        p1_next_state = next_state[0]
        p2_next_state = next_state[1]
        
        p1_action_deque.append(p1_action)
        p2_action_deque.append(p2_action)

        # Direction of the ball
        ball_dir = np.argmax(p1_state[4:10])
        ball_dir_next = np.argmax(p1_next_state[4:10])

        p1_reward = reward[0]
        p2_reward = reward[1]
        if np.any(reward):
            episode_length_list.append(tag_interval_length)
            tag_interval_length = 0

        p1_reward_deque.append(p1_reward)
        p2_reward_deque.append(p2_reward)


        # Store (state, action, reward, next_state) to Replay Buffer for Reinforcement Learning
        if len(p1_state_deque) == args.frame_skipping or np.any(reward):
            deque_state = p1_state_deque[0]
            deque_reward = multi_step_reward(p1_reward_deque, args.gamma)
            deque_action = p1_action_deque[0]
            p1_replay_buffer.push(deque_state, deque_action, deque_reward, p1_next_state, np.float32(done[0]))

            deque_state = p2_state_deque[0]
            deque_reward = multi_step_reward(p2_reward_deque, args.gamma)
            deque_action = p2_action_deque[0]
            p2_replay_buffer.push(deque_state, deque_action, deque_reward, p2_next_state, np.float32(done[1]))

        
        # Store (state, action) to Reservoir Buffer for Supervised Learning if agent
        # follows best response policy
        if is_best_response:
            p1_reservoir_buffer.push(p1_state, p1_action)
            p2_reservoir_buffer.push(p2_state, p2_action)

        (p1_state, p2_state) = (p1_next_state, p2_next_state)

        # Logging
        p1_episode_reward += p1_reward
        p2_episode_reward += p2_reward
        tag_interval_length += 1

        # Episode done. Reset environment and clear logging records
        if all(done) or tag_interval_length >= args.max_tag_interval:
            state = env.reset()
            p1_state = state[0]; p2_state = state[1]
            p1_reward_list.append(p1_episode_reward)
            p2_reward_list.append(p2_episode_reward)
            length_list.append(np.mean(episode_length_list))
            writer.add_scalar("p1/episode_reward", p1_episode_reward, frame_idx)
            writer.add_scalar("p2/episode_reward", p2_episode_reward, frame_idx)
            writer.add_scalar("data/average_frames_per_round", np.mean(episode_length_list), frame_idx)
            p1_episode_reward, p2_episode_reward, tag_interval_length = 0, 0, 0
            p1_reward, p2_reward = 0, 0
            episode_length_list.clear()
            p1_state_deque.clear(), p2_state_deque.clear()
            p1_reward_deque.clear(), p2_reward_deque.clear()
            p1_action_deque.clear(), p2_action_deque.clear()


        if (len(p1_replay_buffer) > args.rl_start and
            len(p1_reservoir_buffer) > args.sl_start and
            frame_idx % args.train_freq == 0):
            # Update Best Response with Reinforcement Learning
            if args.ddqn:
                loss = compute_rl_loss_DDQN(p1_current_model, p1_target_model, p1_replay_buffer, p1_rl_optimizer, args)
            else:
                loss = compute_rl_loss(p1_current_model, p1_target_model, p1_replay_buffer, p1_rl_optimizer, args)
            p1_rl_loss_list.append(loss.item())
            writer.add_scalar("p1/rl_loss", loss.item(), frame_idx)

            if args.ddqn:
                loss = compute_rl_loss_DDQN(p2_current_model, p2_target_model, p2_replay_buffer, p2_rl_optimizer, args)
            else:
                loss = compute_rl_loss(p2_current_model, p2_target_model, p2_replay_buffer, p2_rl_optimizer, args)
            p2_rl_loss_list.append(loss.item())
            writer.add_scalar("p2/rl_loss", loss.item(), frame_idx)

            # Update Average Strategy with Supervised Learning
            loss = compute_sl_loss(p1_policy, p1_reservoir_buffer, p1_sl_optimizer, args)
            p1_sl_loss_list.append(loss.item())
            writer.add_scalar("p1/sl_loss", loss.item(), frame_idx)

            loss = compute_sl_loss(p2_policy, p2_reservoir_buffer, p2_sl_optimizer, args)
            p2_sl_loss_list.append(loss.item())
            writer.add_scalar("p2/sl_loss", loss.item(), frame_idx)
        

        if frame_idx % args.update_target == 0:
            p1_target_model.load_state_dict(p1_current_model.state_dict())
            p2_target_model.load_state_dict(p2_current_model.state_dict())


        # Logging and Saving models
        if frame_idx % args.evaluation_interval == 0:
            print_log(frame_idx, prev_frame, prev_time, (p1_reward_list, p2_reward_list), length_list,
                      (p1_rl_loss_list, p2_rl_loss_list), (p1_sl_loss_list, p2_sl_loss_list))
            p1_reward_list.clear(), p2_reward_list.clear(), length_list.clear()
            p1_rl_loss_list.clear(), p2_rl_loss_list.clear()
            p1_sl_loss_list.clear(), p2_sl_loss_list.clear()
            prev_frame = frame_idx
            prev_time = time.time()
            if frame_idx in [1500000, 1450000, 1400000]:
                save_model(models={"p1": p1_current_model, "p2": p2_current_model},
                        policies={"p1": p1_policy, "p2": p2_policy}, args=args, frame_idx=frame_idx)
        
        # Render if rendering argument is on
        if args.render:
            env.render()
            # sleep(0.5)


def multi_step_reward(rewards, gamma):
    ret = 0.
    for idx, reward in enumerate(rewards):
        ret += reward * (gamma ** idx)
    return ret