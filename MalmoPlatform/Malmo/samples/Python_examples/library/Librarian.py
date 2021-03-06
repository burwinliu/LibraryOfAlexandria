import json
import time
from random import random, randint

import numpy
import os

import gym, ray
from gym.spaces import Discrete, Box
import matplotlib.pyplot as plt
from ray.rllib.agents import ppo
import sys

try:
    import setup
except ModuleNotFoundError:
    print("no setup found, ignoring")

try:
    from malmo import MalmoPython
except ImportError:
    import MalmoPython

import matplotlib
from Requester import Requester

matplotlib.use('TKAgg')


class Librarian(gym.Env):
    def __init__(self, env_config):
        self.agent = MalmoPython.AgentHost()
        # env_config contains info, including items, etc.

        # These are the dict of items that are to be distributed, key = item; value = number of item
        self._env_items = env_config['items']
        # number of chests
        self.obs_size = env_config['chestNum']
        # map to enable one-hot mapping
        self.map = env_config['mapping']
        # reverse mapping
        self.rMap = env_config['rmapping']
        # maximum items per chest to enqable
        self.max_items_per_chest = env_config['max_per_chest']

        # Contents of chests key = item; value = set of items positions
        self._itemPos = {}
        self._placingInventory = []
        self._chestContents = []

        # Ideas: record next open slot per chest
        self._chestPosition = []
        # Percentage for failure to open in a chest
        self._stochasticFailure = env_config['_stochasticFailure']
        self._inventory = {}
        self._nextOpen = 0
        self._log_freq = 10
        self.directory = env_config['directoryName']
        # model params
        self.action_tracker = {}
        self._episode_score = 0
        self.agent_position = 0
        self.episode_number = 0

        # Data saves
        self.returnData = env_config['returnData']
        self.stepData = env_config['stepData']
        self.itemData = env_config['itemData']
        self.failureData = env_config['failureData']

        self.inv_number = 0
        self.item = 0
        self.obs = numpy.zeros(shape=(self.obs_size + 1, self.max_items_per_chest, len(self._env_items)))
        self.world_obs = None
        self.heatmap = numpy.zeros(shape=(len(self._env_items), self.obs_size))
        # self._input_dist = sorted(numpy.random.random((len(self._env_items),)))

        # required for RLLib
        self.action_space = Discrete(self.obs_size)
        self.observation_space = Box(0, 1,
                                     shape=((self.obs_size + 1) * self.max_items_per_chest * len(self._env_items),),
                                     dtype=numpy.float32)
        # For quick training
        self._display = env_config['_display']
        self._print_logs = env_config['_print_logs']
        self._sleep_interval = env_config['_sleep_interval']

        #  todo code class for requester
        # nondeterm situation occuring when get reward at times
        self._requester = env_config['requester']

    def _optimal_retrieve(self, input: dict):
        """
            input: dict of objects to retrieve in format of {key: object_id, value: number to retrieve}
            Assumed that the self._itemPos is properly updated and kept done well
        """
        if self._print_logs:
            print(self._itemPos)
            print(self._chestContents)
        action_plan = []
        result = {}
        for item_id, num_retrieve in input.items():
            if len(self._itemPos[item_id]) > 0:
                # Therefore can retrieve, else you dun messed up
                pq_items = sorted([i for i in self._itemPos[item_id]])
                if self._print_logs:
                    print(self._itemPos)
                    print(self._chestContents)
                    print(pq_items)
                    print(num_retrieve)
                    print(item_id)
                # Now we pop until we find
                while num_retrieve > 0 and len(pq_items) > 0:
                    toConsider = pq_items.pop()
                    if random() < self._stochasticFailure[toConsider]:
                        continue
                    chest = self._chestContents[toConsider]
                    if num_retrieve <= len(chest[item_id]):
                        toRetrieve = num_retrieve
                    else:
                        toRetrieve = len(chest[item_id])
                    action_plan.append((toConsider, item_id, toRetrieve))
                    if item_id not in result:
                        result[item_id] = 0
                    result[item_id] += toRetrieve
                    num_retrieve -= toRetrieve

        action_plan = sorted(action_plan, key=lambda x: x[0])  # Sort by the first elemetn in the tuple
        score = 0
        for position, item, num_retrieve in action_plan:
            # Should be in order from closest to furthest and retreiving the items so we should be able to execute
            #   from here
            score += self.moveToChest(position + 1)
            score += self.openChest()
            self.getItems({item: num_retrieve})
            score += self.closeChest()
        score += self.moveToChest(0)
        if self._display:
            score += self.openChest()
            # Max position item should be at
            for i in range(self._nextOpen):
                self.invAction("swap", i, i)
            score += self.closeChest()
        return result, score

    def step(self, action):
        """
        Take an action in the environment and return the results.

        Args
            action: <int> index of the action to take

        Returns
            observation: <np.array> flattened array of obseravtion
            reward: <int> reward from taking action
            done: <bool> indicates terminal state
            info: <dict> dictionary of extra information
        TODO Add step counter, after N amount of steps, (100) if it fails to place, then provide negative rewards 50
        """
        # item to be placed
        
        if self._print_logs:
            print(f" ACTION {action}, {self.action_space}, {self.observation_space}")
            print(self.inv_number)
            print(self.item)
        if action == 0:
            action = self.obs_size
        if action not in self.action_tracker:
            self.action_tracker[action] = 0
        self.action_tracker[action] += 1
        
        reward = 0
        if self._display:
            time.sleep(self._sleep_interval)
        self.moveToChest(action)
        self.openChest()

        placed = False
        # new observation
        for i, x in enumerate(self.obs[self.agent_position]):
            if not any(self.obs[self.agent_position][i]):
                if self._print_logs:
                    print(self.obs[self.agent_position][i])
                if self._display:
                    self.invAction("swap", self.inv_number, i)
                self.obs[self.agent_position][i][self.item] = 1
                self._itemPos[self.rMap[self.item]].add(self.agent_position - 1)
                self._chestContents[self.agent_position - 1][self.rMap[self.item]].append(i)
                self.heatmap[self.item][self.agent_position-1] += 1/ (self._env_items[self.rMap[self.item]]/64)

                # clear since item has been placed
                self.obs[0][0] = numpy.zeros(shape=len(self._env_items))
                placed = True
                break

        if self._display:
            time.sleep(1)

        self.closeChest()
        done = False
        if placed:
            if self._display:
                if self.world_obs:
                    for x in self.world_obs:
                        if "Inventory" in x and "item" in x:
                            if self.world_obs[x] != 'air':
                                self.inv_number = int(x.split("_")[1])
                                self.item = self.map[self.world_obs[x]]
                                # set next item to be place
                                self.obs[0][0][self.item] = 1
                                break
                    else:
                        # if for loop doesn't break that means only air was found we are done and compute final reward
                        done = True
                        self.moveToChest(0)
                        to_retrieve = self._requester.get_request()
                        retrieved_items, score = self._optimal_retrieve(to_retrieve)
                        reward, failed = self._requester.get_reward(to_retrieve, retrieved_items, score)
                        self.stepData.append(score)
                        self.failureData.append(failed)
            else:
                # simulated inventory
                for i, x in enumerate(self._placingInventory):
                    if x != -1:
                        self.item = x
                        self.inv_number = i
                        self._placingInventory[i] = -1
                        self.obs[0][0][self.item] = 1
                        break
                else:
                    done = True
                    self.moveToChest(0)
                    to_retrieve = self._requester.get_request()
                    retrieved_items, score = self._optimal_retrieve(to_retrieve)
                    reward, failed = self._requester.get_reward(to_retrieve, retrieved_items, score)
                    self.stepData.append(score)
                    self.failureData.append(failed)
        else:
            return self.obs.flatten(), -5, done, dict()
        if self._print_logs:
            print(self.obs)
        if done:
            # end malmo mission
            self.moveToChest(-1)
            self._episode_score += reward
            done = not self._display
            while not done:
                world_state = self.agent.getWorldState()
                for error in world_state.errors:
                    print("Error:", error.text)
                done = not world_state.is_mission_running
                time.sleep(self._sleep_interval)
        if self._print_logs:
            print(done)

        # 0 reward if no retrieve
        return self.obs.flatten(), reward, done, dict()

    def GetMissionXML(self):
        leftX = self.obs_size * 2 + 2
        front = f"<DrawCuboid x1='{leftX}' y1='0' z1='2' x2='-4' y2='10' z2='2' type='bookshelf' />"
        right = f"<DrawCuboid x1='-4' y1='0' z1='2' x2='-4' y2='10' z2='-10' type='bookshelf' />"
        left = f"<DrawCuboid x1='{leftX}' y1='0' z1='2' x2='{leftX}' y2='10' z2='-10' type='bookshelf' />"
        back = f"<DrawCuboid x1='{leftX}' y1='0' z1='-10' x2='-4' y2='10' z2='-10' type='bookshelf' />"
        floor = f"<DrawCuboid x1='{leftX}' y1='1' z1='-10' x2='-4' y2='1' z2='2' type='bookshelf' />"
        libraryEnv = front + right + left + back + floor
        item = f""
        for items in self._env_items:
            for x in range(self._env_items[items]):
                item += f"<DrawItem x='0' y='3' z='1' type='{items}' />"
        chests = f"<DrawBlock x='0' y='2' z='1' type='air' />" + \
                 f"<DrawBlock x='0' y='2' z='1' type='chest' />"
        for chest_num in range(self.obs_size):
            chests += f"<DrawBlock x='{chest_num * 2 + 2}' y='2' z='1' type='air' />"
            chests += f"<DrawBlock x='{chest_num * 2 + 2}' y='2' z='1' type='chest' />"
            chests += f"<DrawBlock x='{chest_num * 2 + 2}' y='1' z='0' type='diamond_block' />"
        chests += f"<DrawBlock x='0' y='2' z='1' type='chest' />"

        return f'''<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
                        <Mission xmlns="http://ProjectMalmo.microsoft.com" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">

                            <About>
                                <Summary>Librarian</Summary>
                            </About>

                            <ServerSection>
                                <ServerInitialConditions>
                                    <Time>
                                        <StartTime>12000</StartTime>
                                        <AllowPassageOfTime>false</AllowPassageOfTime>
                                    </Time>
                                    <Weather>clear</Weather>
                                </ServerInitialConditions>
                                <ServerHandlers>
                                    <FlatWorldGenerator generatorString="3;7,2;1;"/>
                                    <DrawingDecorator>
                                        {libraryEnv}
                                        {item}
                                        {chests}
                                        <DrawBlock x='-2' y='1' z='0' type='iron_block' />
                                        <DrawBlock x='0' y='1' z='0' type='emerald_block' />
                                    </DrawingDecorator>
                                    <ServerQuitWhenAnyAgentFinishes/>     
                                </ServerHandlers>
                            </ServerSection>

                            <AgentSection mode="Survival">
                                <Name>Librarian</Name>
                                <AgentStart>
                                    <Placement x="0.5" y="3" z="0.5" pitch="40" yaw="0"/>
                                    <Inventory>
                                    </Inventory>
                                </AgentStart>
                                <AgentHandlers>
                                    <ContinuousMovementCommands/>
                                    <DiscreteMovementCommands/>
                                    <ChatCommands/>
                                    <ObservationFromFullStats/>
                                    <InventoryCommands/>
                                    <ObservationFromFullInventory/>
                                    <ObservationFromRay/>
                                    <ObservationFromGrid>
                                        <Grid name="floorAll">
                                            <min x="-{int(self.obs_size / 2)}" y="-1" z="-{int(self.obs_size / 2)}"/>
                                            <max x="{int(self.obs_size / 2)}" y="0" z="{int(self.obs_size / 2)}"/>
                                        </Grid>
                                    </ObservationFromGrid>
                                    <AgentQuitFromTouchingBlockType>
                                        <Block type="iron_block"/>
                                    </AgentQuitFromReachingPosition>

                                </AgentHandlers>
                            </AgentSection>
                        </Mission>'''

    def _updateObs(self):
        if not self._display:
            return
        toSleep = .1
        self.world_obs = None
        while self.world_obs is None:
            time.sleep(toSleep)
            toSleep += .2
            try:
                cur_state = self.agent.getWorldState()
                self.world_obs = json.loads(cur_state.observations[-1].text)
            except IndexError:
                print("retrying...")

    # Primative move actions
    def moveLeft(self, steps, force):
        if self._display or force:
            for i in range(steps):
                self.agent.sendCommand("moveeast")
                time.sleep(self._sleep_interval)
        return steps

    def moveRight(self, steps, force):
        if self._display or force:
            for i in range(steps):
                self.agent.sendCommand("movewest")
                time.sleep(self._sleep_interval)
        return steps

    def openChest(self):
        if self._display:
            self.agent.sendCommand("use 1")
            time.sleep(self._sleep_interval)
            self.agent.sendCommand("use 0")
            time.sleep(self._sleep_interval)
        return 1

    def closeChest(self):
        if self._display:
            for _ in range(10):
                self.agent.sendCommand("movenorth")
            time.sleep(self._sleep_interval)
            for _ in range(10):
                self.agent.sendCommand("movesouth")
            time.sleep(self._sleep_interval)
        return 1

    # Complex Move actions
    def moveToChest(self, chest_num, force=False):

        if self.agent_position == chest_num:
            return 0
        if chest_num != -1 and self._print_logs:
            print(f"Moving to chest #{chest_num} ..")
        if self.agent_position - chest_num < 0:
            result = self.moveLeft(2 * abs(self.agent_position - chest_num), force)
        else:
            result = self.moveRight(2 * abs(self.agent_position - chest_num), force)
        self.agent_position = chest_num
        time.sleep(self._sleep_interval)
        return result

    def invAction(self, action, inv_index, chest_index):
        self._updateObs()
        if "inventoriesAvailable" in self.world_obs:
            chestName = self.world_obs["inventoriesAvailable"][-1]['name']
            self.agent.sendCommand(f"{action}InventoryItems {inv_index} {chestName}:{chest_index}")
            time.sleep(self._sleep_interval)
            self._updateObs()

    def getItems(self, query):
        """
            query = dict{ key = itemId: value = number to retrieve }
        """
        chest = self._chestContents[self.agent_position - 1]
        for itemId, toRetrieve in query.items():
            for i in range(toRetrieve):
                try:
                    posToGet = chest[itemId].pop()
                except IndexError:
                    print("Bad retrieval, should not have happened, somewhere we did not update properly")
                    break
                # Create a new slot for this new item, and deposit there
                if itemId not in self._inventory:
                    self._inventory[itemId] = set()
                self._inventory[itemId].add(self._nextOpen)
                if self._display:
                    self.invAction("swap", self._nextOpen, posToGet)
                self._nextOpen += 1

                time.sleep(self._sleep_interval)
            # update if we have retrieved all said items within the chest
            if len(chest[itemId]) == 0:
                del self._chestContents[self.agent_position - 1][itemId]
                self._itemPos[itemId].remove(self.agent_position - 1)

    def reset(self):
        """
        Resets the environment for the next episode.

        Returns
            observation: <np.array> flattened initial obseravtion
        """
        # Reset Malmo
        self.episode_number += 1
        if self._display:
            self.init_malmo()
        time.sleep(1)
        self.obs = numpy.zeros(shape=(self.obs_size + 1, self.max_items_per_chest, len(self._env_items)))
        self.returnData.append(self._episode_score)
        if self._print_logs:
            print(self.returnData)
        if self.episode_number % self._log_freq == 0:
            self.log()
        self._episode_score = 0
        self.agent_position = 0
        self._placingInventory = []
        self._updateObs()
        if self._display:
            for x in self.world_obs:
                if "Inventory" in x and "item" in x:
                    if self._display:
                        if self.world_obs[x] != 'air':
                            self.inv_number = int(x.split("_")[1])
                            self.item = self.map[self.world_obs[x]]
                            break
                    else:
                        if self.world_obs[x] in self.map:
                            self._placingInventory.append(self.map[self.world_obs[x]])
                        else:
                            self._placingInventory.append(-1)
        else:
            self._placingInventory = [-1] * 40
            pos = 0
            for i in self._env_items:
                toPlace = self._env_items[i]
                while toPlace > 0:
                    self._placingInventory[pos] = self.map[i]
                    toPlace -= 64
                    pos += 1
        self._itemPos = {}
        for items in self.map:
            self._itemPos[items] = set()
        self._chestContents = []
        for chests in range(self.obs_size):
            self._chestContents.append({})
            for items in self.map:
                self._chestContents[chests][items] = []
        self._inventory = {}
        self._nextOpen = 0
        self.obs[0][0][self.item] = 1


        return self.obs.flatten()

    def log(self):
        # Todo, store steps taken over the whole time, number of invalid actions taken, associate item
        #   with placement position
        # TODO Graph failureData, itemDistribution per hundered, moving averages
        if self.episode_number % 100 == 0:
            plt.clf()
            plt.hist(self.returnData[self.episode_number - 100 + 1:self.episode_number])
            plt.title('Reward Distribution at ' + str(self.episode_number))
            plt.ylabel('Occurance')
            plt.xlabel('Reward')
            plt.savefig(f"{self.directory}/reward_histogram{str(self.episode_number)}.png")

            plt.clf()
            plt.hist(self.stepData[self.episode_number - 100 + 1:self.episode_number])
            plt.title('Steps at ' + str(self.episode_number))
            plt.ylabel('Occurance')
            plt.xlabel('Steps')
            plt.savefig(f"{self.directory}/step_histogram{str(self.episode_number)}.png")

            # Save data
            with open(f"{self.directory}/returnsfinalpart.json", 'w') as f:
                toSave = {}
                for step, value in enumerate(self.returnData[1:]):
                    toSave[int(step)] = int(value)
                json.dump(toSave, f)
            with open(f"{self.directory}/stepData.json", 'w') as f:
                toSave = {}
                for step, value in enumerate(self.stepData[1:]):
                    toSave[int(step)] = int(value)
                json.dump(toSave, f)
            with open(f"{self.directory}/failureData.json", 'w') as f:
                toSave = {}
                for step, value in enumerate(self.failureData[1:]):
                    toSave[int(step)] = int(value)
                json.dump(toSave, f)

            plt.clf()
            plt.bar(self.action_tracker.keys(), self.action_tracker.values())
            plt.title('Action Distribution at ' + str(self.episode_number))
            plt.ylabel('Occurance')
            plt.xlabel('Action')
            plt.savefig(f"{self.directory}/action_barchart{str(self.episode_number)}.png")
            plt.clf()
            items = [k for k, v in sorted(self.map.items(), key=lambda item: item[1])]
            plt.yticks(ticks=numpy.arange(len(items)), labels=items)
            plt.xticks(ticks=numpy.arange(self.obs_size), labels=range(1, self.obs_size + 1), rotation=90)
            saved=plt.imshow(self.heatmap, cmap='Blues',interpolation="nearest")
            plt.colorbar(saved)
            plt.savefig(f"{self.directory}/heatmap{str(self.episode_number)}.png")

            self.action_tracker = {}
            self.heatmap = numpy.zeros(shape=(len(self._env_items), self.obs_size))


        box = numpy.ones(self._log_freq) / self._log_freq
        returns_smooth = numpy.convolve(self.returnData[1:], box, mode='same')
        plt.clf()
        plt.plot(returns_smooth)
        plt.title('Librarian')
        plt.ylabel('Reward')
        plt.xlabel('Episodes')
        plt.savefig(f"{self.directory}/smooth_returns.png")

        box = numpy.ones(self._log_freq) / self._log_freq
        steps_smooth = numpy.convolve(self.stepData[1:], box, mode='same')
        plt.clf()
        plt.plot(steps_smooth)
        plt.title('Librarian')
        plt.ylabel('Steps')
        plt.xlabel('Episodes')
        plt.savefig(f"{self.directory}/steps_smooth.png")

        plt.clf()
        plt.plot(self.failureData)
        plt.title('Librarian')
        plt.ylabel('Failures')
        plt.xlabel('Episodes')
        plt.savefig(f"{self.directory}/failure_data.png")

    def init_malmo(self):
        """
        Initialize new malmo mission.
        """
        if not self._display:
            return
        my_mission = MalmoPython.MissionSpec(self.GetMissionXML(), True)
        my_mission_record = MalmoPython.MissionRecordSpec()
        my_mission.requestVideo(800, 500)
        my_mission.setViewpoint(1)

        max_retries = 3
        my_clients = MalmoPython.ClientPool()
        my_clients.add(MalmoPython.ClientInfo('127.0.0.1', 10000))  # add Minecraft machines here as available

        for retry in range(max_retries):
            try:

                time.sleep(3)
                self.agent.startMission(my_mission, my_clients, my_mission_record, 0,
                                        'Librarian' + str(self.episode_number))
                break
            except RuntimeError as e:
                if retry == max_retries - 1:
                    print("Error starting mission:", e)
                    exit(1)
                else:
                    time.sleep(2)

        world_state = self.agent.getWorldState()
        while not world_state.has_mission_begun:
            time.sleep(0.1)
            world_state = self.agent.getWorldState()
            for error in world_state.errors:
                print("\nError:", error.text)

        return world_state


if __name__ == '__main__':
    # ray.shutdown()
    ray.init()
    # Max request items, valid items, difficulty level
    # TODO: Create a benchmark with a "simple method" by averaging all items and showing how it doesnt work
    # Scatterplot, *preferred*  line as moving average  -- (10 cycles and find average) ,
    #   histograms, bin it every 10 cycles (or maybe 50?) -- saving data then mess with plot
    # Data + model to be saved --
    #
    script_dir = os.path.dirname(__file__)

    # req_path = "PATHTO\\library\\logs2\\requester.json "
    # lib_path = "PATHTO\\library\\logs2\\checkpoint_1102\\checkpoint-1102"
    # return_path = "PATHTO\\library\\logs2\\returnsfinalpart.json "
    # step_path = "PATHTO\\library\\logs2\\stepData.json "

    req_path = os.path.join(script_dir, "requester.json")
    # req_path = None
    lib_path = None
    return_path = None
    step_path = None
    failure_path = None

    log_number = ""
    MAX_ITEMS = 5
    COMPLEXITY_LEVEL = 2
    if log_number == "":
        logs_count = 0
        for files in os.listdir():
            if os.path.isdir(files) and 'logs' in files:
                logs_count += 1
        log_number = 'logs' + str(logs_count)
    try:
        os.mkdir(log_number)
    except FileExistsError:
        print("RESUMING RUN WELCOME BACK")
    # _stochasticFailure = [i * .1 for i in numpy.random.random(10)]
    # for i in range(3):
    #     _stochasticFailure[randint(0, 9)] /= .1
    returnData = []
    stepData = []
    itemData = {}
    failureData = []
    if return_path is not None:
        with open(return_path) as json_file:
            returnData = [i for i in json.load(json_file).values()]
    if step_path is not None:
        with open(step_path) as json_file:
            stepData = [i for i in json.load(json_file).values()]
    if failure_path is not None:
        with open(failure_path) as json_file:
            failureData = [i for i in json.load(json_file).values()]
    env = {
        'items': {'stone': 128, 'diamond': 64, 'glass': 64, 'ladder': 128, 'brick': 64, 'dragon_egg': 128 * 3},
        'mapping': {'stone': 0, 'diamond': 1, 'glass': 2, 'ladder': 3, 'brick': 4, 'dragon_egg': 5},
        'rmapping': {0: 'stone', 1: 'diamond', 2: 'glass', 3: 'ladder', 4: 'brick', 5: 'dragon_egg'},
        'chestNum': 10,
        'max_per_chest': 3,
        'directoryName': log_number,
        '_display': False,
        '_print_logs': False,
        '_sleep_interval': 0,
        'returnData': returnData,
        'stepData': stepData,
        'itemData': itemData,
        'failureData': failureData,
        # For benchmarking, holding constant
        # Worse case scenario
        # Todo Show all 3 cases then, and graph step time
        # 0 reward fails to retrieve items; + retrieving items -factor (num of steps)
        # '_stochasticFailure': [0] * 10
        '_stochasticFailure': [0.7805985575324255, 0.010020667324609045, 0.618243240812539, 0.06541976810436156,
                               0.014450713025995533, 0.05572127466323378, 0.04338720075449303, 0.007890235534481071,
                               0.01715813232043357, 0.30471561338685693],
        # Medium Case scenario
        # '_stochasticFailure': [0.010020667324609045, 0.7805985575324255, 0.06541976810436156, 0.618243240812539,
        #                        0.014450713025995533, 0.05572127466323378, 0.04338720075449303, 0.007890235534481071,
        #                        0.01715813232043357, 0.30471561338685693],
        # Best Case scenario
        # '_stochasticFailure': [0.010020667324609045, 0.06541976810436156, 0.014450713025995533,
        #                        0.05572127466323378, 0.04338720075449303, 0.007890235534481071, 0.01715813232043357,
        #                        0.618243240812539, 0.7805985575324255, 0.30471561338685693]
        # For true randomness
        # '_stochasticFailure': _stochasticFailure
    }

    if req_path is None:
        env['requester'] = Requester(MAX_ITEMS, env['items'], COMPLEXITY_LEVEL)
    else:
        env['requester'] = Requester(None, None, None, req_path)

    trainer = ppo.PPOTrainer(env=Librarian, config={
        'env_config': env,  # No environment parameters to configure
        'framework': 'torch',  # Use pyotrch instead of tensorflow
        'num_gpus': 0,  # We aren't using GPUs
        'num_workers': 0  # We aren't using parallelism
    })

    if lib_path is not None:
        trainer.restore(lib_path)

    i = 0
    try:
        while True:
            i += 1
            print(trainer.train(), "TRAINING")
            if i % 100 == 0:
                print(f"LIBRARIAN SAVED AT: {trainer.save(log_number)}")
                print(f"REQUESTER SAVED AT: {env['requester'].save_requester(log_number + '/requester.json')}")
                # TODO, change this to save the failure to json file and loading there, or something along those lines
                print(env['_stochasticFailure'])
    finally:
        print(f"LIBRARIAN SAVED AT: {trainer.save(log_number)}")
        print(f"REQUESTER SAVED AT: {env['requester'].save_requester(log_number + '/requester.json')}")
        # TODO, change this to save the failure to json file and loading there, or something along those lines
        print(env['_stochasticFailure'])
