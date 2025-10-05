
def main(parameters):
    import torch
    import logging
    import torch.nn as nn
    import torch.nn.functional as F
    import torch.distributed as dist
    
    from torch.utils.data import DataLoader, Dataset
    from torch.nn.parallel import DistributedDataParallel as DDP
    from torch.utils.data.distributed import DistributedSampler
    import torchvision.transforms as transforms
    from torchvision.datasets import MNIST
    import os
    
    log_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", "%Y-%m-%dT%H:%M:%SZ"
    )
    logger = logging.getLogger(__file__)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)

    epochs=parameters["epochs"]
    save_every=parameters["save_every"]
    batch_size=parameters["batch_size"]
    lr=parameters["lr"]
    dataset_path=parameters["dataset_path"]
    snapshot_path=parameters["snapshot_path"]
    backend=parameters["backend"]
        
    def ddp_setup(backend="nccl"):
        """Setup for Distributed Data Parallel with specified backend."""
        if torch.cuda.is_available() and backend=="nccl":
            num_devices = torch.cuda.device_count()
            device = int(os.environ.get("LOCAL_RANK", 0))  # Default to device 0
            if device >= num_devices:
                logger.warning(f"Warning: Invalid device ordinal {device}. Defaulting to device 0.")
                device = 0
            torch.cuda.set_device(device)
        else:
            logger.info("No GPU available, falling back to CPU.")
            backend="gloo"
        dist.init_process_group(backend=backend)
    
    class Net(nn.Module):
        def __init__(self):
            super(Net, self).__init__()
            self.conv1 = nn.Conv2d(1, 20, 5, 1)
            self.conv2 = nn.Conv2d(20, 50, 5, 1)
            self.fc1 = nn.Linear(4 * 4 * 50, 500)
            self.fc2 = nn.Linear(500, 10)
    
        def forward(self, x):
            x = F.relu(self.conv1(x))
            x = F.max_pool2d(x, 2, 2)
            x = F.relu(self.conv2(x))
            x = F.max_pool2d(x, 2, 2)
            x = x.view(-1, 4 * 4 * 50)
            x = F.relu(self.fc1(x))
            x = self.fc2(x)
            return F.log_softmax(x, dim=1)
    
    class Trainer:
        def __init__(
            self,
            model: torch.nn.Module,
            train_data: DataLoader,
            optimizer: torch.optim.Optimizer,
            save_every: int,
            snapshot_path: str,
            backend: str,
        ) -> None:
            self.local_rank = int(os.environ.get("LOCAL_RANK", -1))  # Ensure fallback if LOCAL_RANK isn't set
            self.global_rank = int(os.environ["RANK"])
    
    
            self.model=model
            self.train_data = train_data
            self.optimizer = optimizer
            self.save_every = save_every
            self.epochs_run = 0
            self.snapshot_path = snapshot_path
            self.backend = backend

            # Set up device first (needed for snapshot loading)
            if torch.cuda.is_available() and self.backend=="nccl":
                self.device = torch.device(f'cuda:{self.local_rank}')
            else:
                self.device = torch.device('cpu')
            logger.info(f"Using device: {self.device}")

            # Create directory for snapshot if needed
            snapshot_dir = os.path.dirname(self.snapshot_path)
            if snapshot_dir:  # Only create directory if path has a directory component
                os.makedirs(snapshot_dir, exist_ok=True)

            if os.path.exists(self.snapshot_path):
                logger.info("Loading snapshot")
                self._load_snapshot(self.snapshot_path)
    
            # Move model to device and wrap with DDP
            if torch.cuda.is_available() and self.backend=="nccl":
                self.model = DDP(self.model.to(self.device), device_ids=[self.local_rank])
            else:
                self.model = DDP(self.model.to(self.device))
    
        def _run_batch(self, source, targets):
            self.optimizer.zero_grad()
            output = self.model(source)
            loss = F.cross_entropy(output, targets)
            loss.backward()
            self.optimizer.step()
    
        def _run_epoch(self, epoch, backend):
            b_sz = len(next(iter(self.train_data))[0])
            if torch.cuda.is_available() and backend=="nccl":
                logger.info(f"[GPU{self.global_rank}] Epoch {epoch} | Batchsize: {b_sz} | Steps: {len(self.train_data)}")
            else:
                logger.info(f"[CPU{self.global_rank}] Epoch {epoch} | Batchsize: {b_sz} | Steps: {len(self.train_data)}")
            if isinstance(self.train_data.sampler, DistributedSampler):
                self.train_data.sampler.set_epoch(epoch)
            for source, targets in self.train_data:
                source = source.to(self.device)
                targets = targets.to(self.device)
                self._run_batch(source, targets)
    
        def _save_snapshot(self, epoch):
            snapshot = {
                "MODEL_STATE": self.model.module.state_dict() if torch.cuda.is_available() else self.model.state_dict(),
                "EPOCHS_RUN": epoch,
            }
            torch.save(snapshot, self.snapshot_path)
            logger.info(f"Epoch {epoch} | Training snapshot saved at {self.snapshot_path}")
        
        def _load_snapshot(self, snapshot_path):
            snapshot = torch.load(snapshot_path, map_location=self.device)
            self.model.load_state_dict(snapshot["MODEL_STATE"])
            self.epochs_run = snapshot["EPOCHS_RUN"]
            logger.info(f"Resuming training from snapshot. Epochs run: {self.epochs_run}")
            
        def train(self, max_epochs: int, backend: str):
            for epoch in range(self.epochs_run, max_epochs):
                self._run_epoch(epoch, backend)
                if self.global_rank == 0 and epoch % self.save_every == 0:
                    self._save_snapshot(epoch)
    
    
    def load_train_objs(dataset_path: str,lr: float):
        """Load dataset, model, and optimizer."""
        train_set = MNIST(dataset_path,
            train=False,
            download=True,
            transform=transforms.Compose([transforms.ToTensor()]))
        model = Net()
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)
        return train_set, model, optimizer
    
    
    def prepare_dataloader(dataset: Dataset, batch_size: int, useGpu: bool):
        """Prepare DataLoader with DistributedSampler."""
        return DataLoader(
            dataset,
            batch_size=batch_size,
            pin_memory=useGpu,
            shuffle=False,
            sampler=DistributedSampler(dataset)
        )
    
    ddp_setup(backend)
    dataset, model, optimizer = load_train_objs(dataset_path, lr)
    train_loader = prepare_dataloader(dataset, batch_size, torch.cuda.is_available() and backend=="nccl")
    trainer = Trainer(model, train_loader, optimizer, save_every, snapshot_path, backend)
    trainer.train(epochs, backend)
    dist.destroy_process_group()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Distributed MNIST Training")
    parser.add_argument('--epochs', type=int, required=True, help='Total epochs to train the model')
    parser.add_argument('--save_every', type=int, required=True, help='How often to save a snapshot')
    parser.add_argument('--batch_size', type=int, default=64, help='Input batch size on each device (default: 64)')
    parser.add_argument('--lr', type=float, default=1e-3, help='Learning rate (default: 1e-3)')
    parser.add_argument('--dataset_path', type=str, default="../data", help='Path to MNIST datasets (default: ../data)')
    parser.add_argument('--snapshot_path', type=str, default="snapshot_mnist.pt", help='Path to save snapshots (default: snapshot_mnist.pt)')
    parser.add_argument('--backend', type=str, choices=['gloo', 'nccl'], default='nccl', help='Distributed backend type (default: nccl)')
    args = parser.parse_args()

    main(
        epochs=args.epochs,
        save_every=args.save_every,
        batch_size=args.batch_size,
        lr=args.lr,
        dataset_path=args.dataset_path,
        snapshot_path=args.snapshot_path,
        backend=args.backend
    )