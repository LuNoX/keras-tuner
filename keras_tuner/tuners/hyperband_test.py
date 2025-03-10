# Copyright 2019 The KerasTuner Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

import numpy as np
import tensorflow as tf

import keras_tuner
from keras_tuner.engine import tuner_utils
from keras_tuner.tuners import hyperband as hyperband_module


def build_model(hp):
    model = tf.keras.Sequential()
    for i in range(hp.Int("layers", 1, 3)):
        model.add(
            tf.keras.layers.Dense(hp.Int(f"units{str(i)}", 1, 5), activation="relu")
        )

        model.add(
            tf.keras.layers.Lambda(lambda x: x + hp.Float(f"bias{str(i)}", -1, 1))
        )

    model.add(tf.keras.layers.Dense(1, activation="sigmoid"))
    model.compile("sgd", "mse")
    return model


def test_hyperband_oracle_bracket_configs(tmp_path):
    oracle = hyperband_module.HyperbandOracle(
        objective=keras_tuner.Objective("score", "max"),
        hyperband_iterations=1,
        max_epochs=8,
        factor=2,
    )
    oracle._set_project_dir(tmp_path, "untitled")

    # 8, 4, 2, 1 starting epochs.
    assert oracle._get_num_brackets() == 4

    assert oracle._get_num_rounds(bracket_num=3) == 4
    assert oracle._get_size(bracket_num=3, round_num=0) == 8
    assert oracle._get_epochs(bracket_num=3, round_num=0) == 1
    assert oracle._get_size(bracket_num=3, round_num=3) == 1
    assert oracle._get_epochs(bracket_num=3, round_num=3) == 8

    assert oracle._get_num_rounds(bracket_num=0) == 1
    assert oracle._get_size(bracket_num=0, round_num=0) == 4
    assert oracle._get_epochs(bracket_num=0, round_num=0) == 8


def test_hyperband_oracle_one_sweep_single_thread(tmp_path):
    hp = keras_tuner.HyperParameters()
    hp.Float("a", -100, 100)
    hp.Float("b", -100, 100)
    oracle = hyperband_module.HyperbandOracle(
        hyperparameters=hp,
        objective=keras_tuner.Objective("score", "max"),
        hyperband_iterations=1,
        max_epochs=9,
        factor=3,
    )
    oracle._set_project_dir(tmp_path, "untitled")

    score = 0
    for bracket_num in reversed(range(oracle._get_num_brackets())):
        for round_num in range(oracle._get_num_rounds(bracket_num)):
            for _ in range(oracle._get_size(bracket_num, round_num)):
                trial = oracle.create_trial("tuner0")
                assert trial.status == "RUNNING"
                score += 1
                oracle.update_trial(trial.trial_id, {"score": score})
                trial.status = "COMPLETED"
                oracle.end_trial(trial)
            assert len(oracle._brackets[0]["rounds"][round_num]) == oracle._get_size(
                bracket_num, round_num
            )
        assert len(oracle._brackets) == 1

    # Iteration should now be complete.
    trial = oracle.create_trial("tuner0")
    assert trial.status == "STOPPED", oracle.hyperband_iterations
    assert len(oracle.ongoing_trials) == 0

    # Brackets should all be finished and removed.
    assert len(oracle._brackets) == 0

    best_trial = oracle.get_best_trials()[0]
    assert best_trial.score == score


def test_hyperband_oracle_one_sweep_parallel(tmp_path):
    hp = keras_tuner.HyperParameters()
    hp.Float("a", -100, 100)
    hp.Float("b", -100, 100)
    oracle = hyperband_module.HyperbandOracle(
        hyperparameters=hp,
        objective=keras_tuner.Objective("score", "max"),
        hyperband_iterations=1,
        max_epochs=4,
        factor=2,
    )
    oracle._set_project_dir(tmp_path, "untitled")

    # All round 0 trials from different brackets can be run
    # in parallel.
    round0_trials = []
    for i in range(10):
        t = oracle.create_trial(f"tuner{str(i)}")
        assert t.status == "RUNNING"
        round0_trials.append(t)

    assert len(oracle._brackets) == 3

    # Round 1 can't be run until enough models from round 0
    # have completed.
    t = oracle.create_trial("tuner10")
    assert t.status == "IDLE"

    for t in round0_trials:
        oracle.update_trial(t.trial_id, {"score": 1})
        t.status = "COMPLETED"
        oracle.end_trial(t)

    round1_trials = []
    for i in range(4):
        t = oracle.create_trial(f"tuner{str(i)}")
        assert t.status == "RUNNING"
        round1_trials.append(t)

    # Bracket 0 is complete as it only has round 0.
    assert len(oracle._brackets) == 2

    # Round 2 can't be run until enough models from round 1
    # have completed.
    t = oracle.create_trial("tuner10")
    assert t.status == "IDLE"

    for t in round1_trials:
        oracle.update_trial(t.trial_id, {"score": 1})
        t.status = "COMPLETED"
        oracle.end_trial(t)

    # Only one trial runs in round 2.
    round2_trial = oracle.create_trial("tuner0")

    assert len(oracle._brackets) == 1

    # No more trials to run, but wait for existing brackets to end.
    t = oracle.create_trial("tuner10")
    assert t.status == "IDLE"

    oracle.update_trial(round2_trial.trial_id, {"score": 1})
    round2_trial.status = "COMPLETED"
    oracle.end_trial(round2_trial)

    t = oracle.create_trial("tuner10")
    assert t.status == "STOPPED", oracle._current_sweep


def test_hyperband_integration(tmp_path):
    tuner = hyperband_module.Hyperband(
        objective="val_loss",
        hypermodel=build_model,
        hyperband_iterations=2,
        max_epochs=6,
        factor=3,
        directory=tmp_path,
    )

    x, y = np.ones((2, 5)), np.ones((2, 1))
    tuner.search(x, y, validation_data=(x, y))

    # Make sure Oracle is registering new HPs.
    updated_hps = tuner.oracle.get_space().values
    assert "units1" in updated_hps
    assert "bias1" in updated_hps

    tf.get_logger().setLevel(logging.ERROR)

    best_score = tuner.oracle.get_best_trials()[0].score
    best_model = tuner.get_best_models()[0]
    assert best_model.evaluate(x, y) == best_score


def test_hyperband_save_and_restore(tmp_path):
    tuner = hyperband_module.Hyperband(
        objective="val_loss",
        hypermodel=build_model,
        hyperband_iterations=1,
        max_epochs=7,
        factor=2,
        directory=tmp_path,
    )

    x, y = np.ones((2, 5)), np.ones((2, 1))
    tuner.search(x, y, validation_data=(x, y))

    num_trials = len(tuner.oracle.trials)
    assert num_trials > 0
    assert tuner.oracle._current_iteration == 1

    tuner.save()
    tuner.trials = {}
    tuner.oracle._current_iteration = 0
    tuner.reload()

    assert len(tuner.oracle.trials) == num_trials
    assert tuner.oracle._current_iteration == 1


def test_hyperband_load_weights(tmp_path):
    tuner = hyperband_module.Hyperband(
        objective="val_loss",
        hypermodel=build_model,
        hyperband_iterations=1,
        max_epochs=2,
        factor=2,
        directory=tmp_path,
    )
    x, y = np.ones((2, 5)), np.ones((2, 1))
    nb_brackets = tuner.oracle._get_num_brackets()
    assert nb_brackets == 2
    nb_models_round_0 = tuner.oracle._get_size(bracket_num=1, round_num=0)
    assert nb_models_round_0 == 2
    # run the trials for the round 0 (from scratch)
    for _ in range(nb_models_round_0):
        trial = tuner.oracle.create_trial("tuner0")
        result = tuner.run_trial(trial, x, y, validation_data=(x, y))
        tuner.oracle.update_trial(
            trial.trial_id,
            tuner_utils.convert_to_metrics_dict(result, tuner.oracle.objective),
            tuner_utils.get_best_step(result, tuner.oracle.objective),
        )
        trial.status = "COMPLETED"
        tuner.oracle.end_trial(trial)

    # ensure the model run in round 1 is loaded from the best model in round 0
    trial = tuner.oracle.create_trial("tuner0")
    hp = trial.hyperparameters
    assert "tuner/trial_id" in hp
    new_model = tuner._try_build(hp)
    assert new_model.predict(x).shape == y.shape
    # get new model weights
    new_model_weights = new_model.weights.copy()
    # get weights from the best model in round 0
    best_trial_round_0_id = hp["tuner/trial_id"]
    best_hp_round_0 = tuner.oracle.trials[best_trial_round_0_id].hyperparameters
    best_model_round_0 = tuner._try_build(best_hp_round_0)
    best_model_round_0.load_weights(
        tuner._get_checkpoint_fname(best_trial_round_0_id)
    )
    assert best_model_round_0.predict(x).shape == y.shape
    best_model_round_0_weights = best_model_round_0.weights.copy()
    # compare the weights
    assert len(new_model_weights) == len(best_model_round_0_weights)
    assert all(
        np.alltrue(new_weight == best_old_weight)
        for new_weight, best_old_weight in zip(
            new_model_weights, best_model_round_0_weights
        )
    )
