from src.selfplay.naive_selfplay_training import learn_with_selfplay


def main():
    # Settings for training and evaluation
    max_agents = 5
    num_eval_eps = 5
    num_skip_steps_list = [0]
    model_type = 'dqn'
    num_learn_steps_list = [1000]
    num_learn_steps_pre_training = 1000
    only_rule_based_opponent_list = [False]  # True forces play against rule_based, i.e. no self-play
    patience = 20

    for only_rule_based_opponent in only_rule_based_opponent_list:
        # if not only_rule_based_opponent:
        #     max_agents = 40
        for num_learn_steps in num_learn_steps_list:
            for num_skip_steps in num_skip_steps_list:
                num_in_k = num_learn_steps / 1000
                # num_name = str(int(num_in_k) if num_in_k.is_integer() else num_in_k)
                # model_name = model_type + '-' + ('' if num_skip_steps == 0 else 'skip-') + num_name + 'k'
                model_name = 'dqn-img-obs'

                print(f'Running training for model {model_name}')
                learn_with_selfplay(max_agents=max_agents,
                                    num_learn_steps=num_learn_steps,
                                    num_learn_steps_pre_training=num_learn_steps_pre_training,
                                    num_eval_eps=num_eval_eps,
                                    num_skip_steps=num_skip_steps,
                                    model_name=model_name,
                                    only_rule_based_op=only_rule_based_opponent,
                                    patience=patience,
                                    image_observations=True)


if __name__ == '__main__':
    main()
