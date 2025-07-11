import random
import numpy as np
import torch

class ReplayBuffer:
    def __init__(self, capacity: int, device: str = "cuda"):
        self.capacity = capacity
        self.device = device
        
        self.states = np.zeros((capacity, 4, 84, 84), dtype=np.uint8)
        self.actions = np.zeros(capacity, dtype=np.int64)
        self.rewards = np.zeros(capacity, dtype=np.float32)
        self.next_states = np.zeros((capacity, 4, 84, 84), dtype=np.uint8)
        self.dones = np.zeros(capacity, dtype=np.float32)
        
        self.position = 0
        self.size = 0

    def push(self, state, action, reward, next_state, done):
        if state.ndim == 3:
            state = np.expand_dims(state, 0)
            next_state = np.expand_dims(next_state, 0)
            action = np.expand_dims(action, 0)
            reward = np.expand_dims(reward, 0)
            done = np.expand_dims(done, 0)

        batch_size = state.shape[0]
        idx = (np.arange(batch_size) + self.position) % self.capacity

        self.states[idx] = state.astype(np.uint8)
        self.actions[idx] = action
        self.rewards[idx] = reward
        self.next_states[idx] = next_state.astype(np.uint8)
        self.dones[idx] = done

        self.position = (self.position + batch_size) % self.capacity
        self.size = min(self.size + batch_size, self.capacity)

    def sample(self, batch_size: int):
        indices = np.random.randint(0, self.size - 1, batch_size, replace=False)
        np.random.shuffle(indices)
        
        states = torch.from_numpy(self.states[indices]).to(self.device, dtype=torch.float32, non_blocking=True) / 255.0
        actions = torch.from_numpy(self.actions[indices]).to(self.device, dtype=torch.long, non_blocking=True)
        rewards = torch.from_numpy(self.rewards[indices]).to(self.device, dtype=torch.float32, non_blocking=True)
        next_states = torch.from_numpy(self.next_states[indices]).to(self.device, dtype=torch.float32, non_blocking=True) / 255.0
        dones = torch.from_numpy(self.dones[indices]).to(self.device, dtype=torch.bool, non_blocking=True)
        
        return states, actions, rewards, next_states, dones

    def __len__(self):
        return self.size

class PrioritizedReplayBuffer(ReplayBuffer):
    def __init__(self, capacity: int, alpha: float = 0.6, device: str = "cuda"):
        super().__init__(capacity, device)
        self.alpha = alpha
        self.priorities = np.zeros((capacity,), dtype=np.float32)
        self.max_priority = 1.0

    def push(self, state, action, reward, next_state, done):
        start = self.position
        super().push(state, action, reward, next_state, done)
        batch_size = state.shape[0] if state.ndim == 4 else 1
        idxs = (np.arange(batch_size) + start) % self.capacity
        self.priorities[idxs] = self.max_priority

    def sample(self, batch_size: int, beta: float = 0.4):
        prios = self.priorities[:self.size]
        probs = prios ** self.alpha
        
        prob_sum = probs.sum()
        if prob_sum == 0:
            probs = np.ones_like(probs) / self.size
        else:
            probs /= prob_sum

        indices = np.random.randint(0, self.size - 1, batch_size, p=probs, replace=False)
        np.random.shuffle(indices)
        
        states = torch.from_numpy(self.states[indices]).to(self.device, dtype=torch.float32) / 255.0
        actions = torch.from_numpy(self.actions[indices]).to(self.device, dtype=torch.long)
        rewards = torch.from_numpy(self.rewards[indices]).to(self.device)
        next_states = torch.from_numpy(self.next_states[indices]).to(self.device, dtype=torch.float32) / 255.0
        dones = torch.from_numpy(self.dones[indices]).to(self.device)

        total = len(self)
        weights = (total * probs[indices]) ** (-beta)
        weights /= (weights.max() + 1e-8)

        return states, actions, rewards, next_states, dones, torch.tensor(weights, dtype=torch.float32, device=self.device), indices

    def update_priorities(self, indices: np.ndarray, new_priorities: np.ndarray):
        for idx, prio in zip(indices, new_priorities):
            priority = min(abs(prio) + 1e-6, 1.0)
            self.priorities[idx] = priority
            self.max_priority = max(self.max_priority, priority)
            
class SumTree():
    def __init__(self, capacity):
        self.capacity = capacity
        self.tree = np.zeros(2 * capacity - 1, dtype=np.float32)
        self.data = [None] * capacity
        self.write = 0
        self.n_entries = 0
        
    def _propagate(self, idx, change):
        parent = (idx - 1) // 2
        self.tree[parent] += change
        if parent != 0:
            self._propagate(parent, change)

    def _retrieve(self, idx, s):
        left = 2 * idx + 1
        right = left + 1
        if left >= len(self.tree):
            return idx
        if s <= self.tree[left]:
            return self._retrieve(left, s)
        else:
            return self._retrieve(right, s - self.tree[left])

    def total(self):
        return self.tree[0]

    def add(self, p, data):
        idx = self.write + self.capacity - 1
        self.data[self.write] = data
        self.update(idx, p)
        self.write = (self.write + 1) % self.capacity
        self.n_entries = min(self.n_entries + 1, self.capacity)

    def update(self, idx, p):
        change = p - self.tree[idx]
        self.tree[idx] = p
        self._propagate(idx, change)

    def get(self, s):
        idx = self._retrieve(0, s)
        data_idx = idx - self.capacity + 1
        return idx, self.tree[idx], self.data[data_idx]
    
class PERBufferSumTree:
    def __init__(self, max_len: int, alpha: float):
        self.tree = SumTree(max_len)
        self.alpha = alpha
        self.epsilon = 1e-6
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.max_priority = 1.0
        
    def push(self, state, action, reward, next_state, done):
        data = (state, action, reward, next_state, done)
        self.tree.add(self.max_priority, data)
        
    def sample(self, batch_size: int, beta: float):
        assert self.tree.n_entries >= batch_size, "Not enough in buffer"
        batch = []
        idxs = []
        priorities = []
        
        while len(batch) < batch_size:
            s = random.uniform(0, self.tree.total())
            idx, p, data = self.tree.get(s)
            
            if data is not None:
                batch.append(data)
                idxs.append(idx)
                priorities.append(p)
        
        states, actions, rewards, next_states, dones = zip(*batch)
        N = self.tree.n_entries
        P = np.array(priorities, dtype=np.float32)
        P_norm = P / self.tree.total()
        weights = (N * P_norm) ** (-beta)
        weights /= weights.max()
        
        states = torch.stack([s.clone().detach() for s in states]).to(self.device)
        actions = torch.tensor(actions, dtype=torch.float32).to(self.device)
        rewards = torch.tensor(rewards, dtype=torch.float32).to(self.device)
        next_states = torch.stack([s.clone().detach() for s in next_states]).to(self.device)
        dones = torch.tensor(dones, dtype=torch.float32).to(self.device)
        weights = torch.as_tensor(weights, dtype=torch.float32).to(self.device)
        
        return states, actions, rewards, next_states, dones, weights, idxs
    
    def __len__(self):
        return self.tree.n_entries
    
    def update_priorities(self, indices, priorities):
        for idx, p in zip(indices, priorities.squeeze(-1)):
            new_p = (abs(p) + self.epsilon) ** self.alpha
            self.tree.update(idx, new_p)
            self.max_priority = max(self.max_priority, new_p)