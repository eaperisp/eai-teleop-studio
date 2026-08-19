from teleop.robot_control.hand_retargeting import HandRetargeting, HandType
from teleop.robot_control.devices.brainco import BraincoHandSDK
from teleop.robot_control.devices.brainco.dds_transport import (
    LEFT_COMMAND_TOPIC,
    LEFT_STATE_TOPIC,
    RIGHT_COMMAND_TOPIC,
    RIGHT_STATE_TOPIC,
)
import numpy as np
from enum import IntEnum
import time
from multiprocessing import Process, Array

import logging_mp
logger_mp = logging_mp.getLogger(__name__)

brainco_Num_Motors = 6
kTopicbraincoLeftCommand = LEFT_COMMAND_TOPIC
kTopicbraincoLeftState = LEFT_STATE_TOPIC
kTopicbraincoRightCommand = RIGHT_COMMAND_TOPIC
kTopicbraincoRightState = RIGHT_STATE_TOPIC


def _update_shared_hand_states(states, left_hand_state_array, right_hand_state_array):
    """Copy each available hand independently; one missing hand must not block the other."""
    for side, shared_array in (
        ("left", left_hand_state_array),
        ("right", right_hand_state_array),
    ):
        state = states.get(side)
        if state is None:
            continue
        with shared_array.get_lock():
            shared_array[:] = state.positions


class Brainco_Controller_ctrl:
    def __init__(self, left_gripper_trigger_in, left_gripper_squeeze_in, right_gripper_trigger_in, right_gripper_squeeze_in, 
                       dual_hand_data_lock = None, dual_hand_state_array = None, dual_hand_action_array = None, fps = 100.0, Unit_Test = False, simulation_mode = False,
                       xr_motion_data_ready_in = None):
        logger_mp.info("Initialize Brainco_Controller_ctrl...")
        self.fps = fps
        self.Unit_Test = Unit_Test
        self.simulation_mode = simulation_mode

        if not self.Unit_Test:
            self.hand_retargeting = HandRetargeting(HandType.BRAINCO_HAND)
        else:
            self.hand_retargeting = HandRetargeting(HandType.BRAINCO_HAND_Unit_Test)

        # Shared Arrays for hand states
        self.left_hand_state_array  = Array('d', brainco_Num_Motors, lock=True)  
        self.right_hand_state_array = Array('d', brainco_Num_Motors, lock=True)

        hand_control_process = Process(target=self.control_process, args=(left_gripper_trigger_in, left_gripper_squeeze_in, right_gripper_trigger_in, right_gripper_squeeze_in, 
                                                                          self.left_hand_state_array, self.right_hand_state_array, dual_hand_data_lock, dual_hand_state_array, dual_hand_action_array,
                                                                          xr_motion_data_ready_in))
        hand_control_process.daemon = True
        hand_control_process.start()

        logger_mp.info("Initialize Brainco_Controller_ctrl OK!\n")

    def ctrl_dual_hand(self, left_q_target, right_q_target):
        """
        Set current left, right hand motor state target q
        """
        self.hand_sdk.command_both(left_q_target, right_q_target)
    
    def control_process(self, left_gripper_trigger_in, left_gripper_squeeze_in, right_gripper_trigger_in, right_gripper_squeeze_in,
                              left_hand_state_array, right_hand_state_array, dual_hand_data_lock = None, dual_hand_state_array = None, dual_hand_action_array = None,
                              xr_motion_data_ready_in = None):
        self.running = True

        left_q_target  = np.full(brainco_Num_Motors, 0.0, dtype=float)
        right_q_target = np.full(brainco_Num_Motors, 0.0, dtype=float)

        self.hand_sdk = BraincoHandSDK(
            "dds",
            sides="both",
            initialize_factory=False,
            continuous_publish=False,
        )
        self.hand_sdk.connect()

        try:
            while self.running:
                start_time = time.time()
                # trigger value range: [10.0, 0.0], 10.0 means no press, 0.0 means full press
                # squeeze value range: [0.0, 1.0],   0.0 means no press, 1.0 means full press
                with left_gripper_trigger_in.get_lock():
                    left_trigger_value = left_gripper_trigger_in.value
                with left_gripper_squeeze_in.get_lock():
                    left_squeeze_value = left_gripper_squeeze_in.value
                with right_gripper_trigger_in.get_lock():
                    right_trigger_value = right_gripper_trigger_in.value
                with right_gripper_squeeze_in.get_lock():
                    right_squeeze_value = right_gripper_squeeze_in.value
                if xr_motion_data_ready_in is not None:
                    with xr_motion_data_ready_in.get_lock():
                        xr_motion_data_ready = xr_motion_data_ready_in.value
                else:
                    xr_motion_data_ready = True

                states = self.hand_sdk.read_states(timeout=0.0)
                _update_shared_hand_states(states, left_hand_state_array, right_hand_state_array)
                state_data = np.concatenate((np.array(left_hand_state_array[:]), np.array(right_hand_state_array[:])))

                if xr_motion_data_ready:
                    # In the official document, the angles are in the range [0, 1] ==> 0.0: fully open  1.0: fully closed
                    left_triger_value = (10.0 - left_trigger_value) / 10.0
                    left_q_target[0]  = np.clip((left_triger_value - 0.5) / 0.5, 0.0, 0.98) # thumb
                    left_q_target[1]  = np.clip(left_triger_value / 0.5, 0.0, 0.7) # thumb-aux
                    left_q_target[2]  = np.clip(left_squeeze_value, 0.0, 0.98)                   # index
                    left_q_target[3]  = np.clip(left_triger_value, 0.0, 0.98)   # middle
                    left_q_target[4]  = np.clip(left_triger_value, 0.0, 0.98)   # ring
                    left_q_target[5]  = np.clip(left_triger_value, 0.0, 0.98)   # pinky

                    right_triger_value = (10.0 - right_trigger_value) / 10.0
                    right_q_target[0] = np.clip((right_triger_value - 0.5) / 0.5, 0.0, 0.98)
                    right_q_target[1] = np.clip(right_triger_value / 0.5, 0.0, 0.7)
                    right_q_target[2] = np.clip(right_squeeze_value, 0.0, 0.98)                  # index
                    right_q_target[3] = np.clip(right_triger_value, 0.0, 0.98)  # middle
                    right_q_target[4] = np.clip(right_triger_value, 0.0, 0.98)  # ring
                    right_q_target[5] = np.clip(right_triger_value, 0.0, 0.98)  # pinky

                # get dual hand state
                action_data = np.concatenate((left_q_target, right_q_target))
                if dual_hand_state_array and dual_hand_action_array:
                    with dual_hand_data_lock:
                        dual_hand_state_array[:] = state_data
                        dual_hand_action_array[:] = action_data

                self.ctrl_dual_hand(left_q_target, right_q_target)
                current_time = time.time()
                time_elapsed = current_time - start_time
                sleep_time = max(0, (1 / self.fps) - time_elapsed)
                time.sleep(sleep_time)
        finally:
            self.hand_sdk.disconnect()
            logger_mp.info("Brainco_Controller_ctrl has been closed.")


class Brainco_Controller_hand:
    def __init__(self, left_hand_array, right_hand_array, dual_hand_data_lock = None, dual_hand_state_array = None,
                       dual_hand_action_array = None, fps = 100.0, Unit_Test = False, simulation_mode = False, xr_motion_data_ready_in = None):
        logger_mp.info("Initialize Brainco_Controller_hand...")
        self.fps = fps
        self.Unit_Test = Unit_Test
        self.simulation_mode = simulation_mode

        if not self.Unit_Test:
            self.hand_retargeting = HandRetargeting(HandType.BRAINCO_HAND)
        else:
            self.hand_retargeting = HandRetargeting(HandType.BRAINCO_HAND_Unit_Test)


        # Shared Arrays for hand states
        self.left_hand_state_array  = Array('d', brainco_Num_Motors, lock=True)  
        self.right_hand_state_array = Array('d', brainco_Num_Motors, lock=True)

        hand_control_process = Process(target=self.control_process, args=(left_hand_array, right_hand_array,  self.left_hand_state_array, self.right_hand_state_array,
                                                                          dual_hand_data_lock, dual_hand_state_array, dual_hand_action_array, xr_motion_data_ready_in))
        hand_control_process.daemon = True
        hand_control_process.start()

        logger_mp.info("Initialize Brainco_Controller_hand OK!")

    def ctrl_dual_hand(self, left_q_target, right_q_target):
        """
        Set current left, right hand motor state target q
        """
        self.hand_sdk.command_both(left_q_target, right_q_target)
    
    def control_process(self, left_hand_array, right_hand_array, left_hand_state_array, right_hand_state_array,
                              dual_hand_data_lock = None, dual_hand_state_array = None, dual_hand_action_array = None, xr_motion_data_ready_in = None):
        self.running = True

        left_q_target  = np.full(brainco_Num_Motors, 0.0, dtype=float)
        right_q_target = np.full(brainco_Num_Motors, 0.0, dtype=float)

        self.hand_sdk = BraincoHandSDK(
            "dds",
            sides="both",
            initialize_factory=False,
            continuous_publish=False,
        )
        self.hand_sdk.connect()

        try:
            while self.running:
                start_time = time.time()
                # get dual hand state
                with left_hand_array.get_lock():
                    left_hand_data  = np.array(left_hand_array[:]).reshape(25, 3).copy()
                with right_hand_array.get_lock():
                    right_hand_data = np.array(right_hand_array[:]).reshape(25, 3).copy()
                if xr_motion_data_ready_in is not None:
                    with xr_motion_data_ready_in.get_lock():
                        xr_motion_data_ready = xr_motion_data_ready_in.value
                else:
                    xr_motion_data_ready = True

                states = self.hand_sdk.read_states(timeout=0.0)
                _update_shared_hand_states(states, left_hand_state_array, right_hand_state_array)

                # Read left and right q_state from shared arrays
                state_data = np.concatenate((np.array(left_hand_state_array[:]), np.array(right_hand_state_array[:])))

                if xr_motion_data_ready:
                    ref_left_value = left_hand_data[self.hand_retargeting.left_indices[1,:]] - left_hand_data[self.hand_retargeting.left_indices[0,:]]
                    ref_right_value = right_hand_data[self.hand_retargeting.right_indices[1,:]] - right_hand_data[self.hand_retargeting.right_indices[0,:]]

                    left_q_target  = self.hand_retargeting.left_retargeting.retarget(ref_left_value)[self.hand_retargeting.left_dex_retargeting_to_hardware]
                    right_q_target = self.hand_retargeting.right_retargeting.retarget(ref_right_value)[self.hand_retargeting.right_dex_retargeting_to_hardware]

                    # In the official document, the angles are in the range [0, 1] ==> 0.0: fully open  1.0: fully closed
                    # The q_target now is in radians, ranges:
                    #     - idx 0:   0~1.52
                    #     - idx 1:   0~1.05
                    #     - idx 2~5: 0~1.47
                    # Normalize the retargeted radians to the Revo2 0.0-1.0 range.
                    def normalize(val, min_val, max_val):
                        return 1.0 - np.clip((max_val - val) / (max_val - min_val), 0.0, 1.0)

                    for idx in range(brainco_Num_Motors):
                        if idx == 0:
                            left_q_target[idx]  = normalize(left_q_target[idx], 0.0, 1.52)
                            right_q_target[idx] = normalize(right_q_target[idx], 0.0, 1.52)
                        elif idx == 1:
                            left_q_target[idx]  = normalize(left_q_target[idx], 0.0, 1.05)
                            right_q_target[idx] = normalize(right_q_target[idx], 0.0, 1.05)
                        elif idx >= 2:
                            left_q_target[idx]  = normalize(left_q_target[idx], 0.0, 1.47)
                            right_q_target[idx] = normalize(right_q_target[idx], 0.0, 1.47)

                # get dual hand action
                action_data = np.concatenate((left_q_target, right_q_target))    
                if dual_hand_state_array and dual_hand_action_array:
                    with dual_hand_data_lock:
                        dual_hand_state_array[:] = state_data
                        dual_hand_action_array[:] = action_data
                # logger_mp.info(f"left_q_target:{left_q_target}")
                self.ctrl_dual_hand(left_q_target, right_q_target)
                current_time = time.time()
                time_elapsed = current_time - start_time
                sleep_time = max(0, (1 / self.fps) - time_elapsed)
                time.sleep(sleep_time)
        finally:
            self.hand_sdk.disconnect()
            logger_mp.info("Brainco_Controller_hand has been closed.")

# according to the official documentation, https://www.brainco-hz.com/docs/revolimb-hand/product/parameters.html
# the motor sequence is as shown in the table below
# ┌──────┬───────┬────────────┬────────┬────────┬────────┬────────┐
# │ Id   │   0   │     1      │   2    │   3    │   4    │   5    │
# ├──────┼───────┼────────────┼────────┼────────┼────────┼────────┤
# │Joint │ thumb │ thumb-aux  |  index │ middle │  ring  │  pinky │
# └──────┴───────┴────────────┴────────┴────────┴────────┴────────┘
class Brainco_Right_Hand_JointIndex(IntEnum):
    kRightHandThumb = 0
    kRightHandThumbAux = 1
    kRightHandIndex = 2
    kRightHandMiddle = 3
    kRightHandRing = 4
    kRightHandPinky = 5

class Brainco_Left_Hand_JointIndex(IntEnum):
    kLeftHandThumb = 0
    kLeftHandThumbAux = 1
    kLeftHandIndex = 2
    kLeftHandMiddle = 3
    kLeftHandRing = 4
    kLeftHandPinky = 5
