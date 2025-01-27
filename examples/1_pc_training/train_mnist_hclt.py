import sys
import pyjuice as juice
import torch
import torch._dynamo as dynamo
import time
import torchvision
import numpy as np
import sys
import logging
import warnings
from torch.utils.data import TensorDataset, DataLoader
import argparse
from typing import Optional
from PIL import Image
from matplotlib import pyplot as plt

warnings.filterwarnings("ignore")
logging.getLogger("torch._inductor.utils").setLevel(logging.ERROR)
logging.getLogger("torch._inductor.compile_fx").setLevel(logging.ERROR)
logging.getLogger("torch._inductor.lowering").setLevel(logging.ERROR)
logging.getLogger("torch._inductor.graph").setLevel(logging.ERROR)

def process_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch_size', type=int, default=512, help='batch_size')
    parser.add_argument('--cuda', type=int, default=0, help='cuda idx')
    parser.add_argument('--num_latents', type=int, default=32, help='num_latents')
    parser.add_argument("--mode", type=str, default="train", help="options: 'train', 'load'")
    parser.add_argument("--dataset", type=str, default="mnist", help="mnist, fashion")
    parser.add_argument("--input_circuit", type=str, default=None, help="load circuit from file instead of learning structure")
    parser.add_argument("--output_dir", type=str, default="examples", help="output directory")
    args = parser.parse_args()
    return args

def evaluate(pc: juice.ProbCircuit, loader: DataLoader, alphas: Optional[torch.Tensor]=None):
    lls_total = 0.0
    for batch in loader:
        x = batch[0].to(pc.device)
        lls = pc(x, alphas=alphas)
        lls_total += lls.mean().detach().cpu().numpy().item()
    
    lls_total /= len(loader)
    return lls_total

def evaluate_miss(pc: juice.ProbCircuit, loader: DataLoader, alphas: Optional[torch.Tensor]=None):
    lls_total = 0.0
    for batch in loader:
        x = batch[0].to(pc.device)
        mask = batch[1].to(pc.device)
        lls = pc(x, missing_mask=mask, alphas=alphas)
        lls_total += lls.mean().detach().cpu().numpy().item()
    
    lls_total /= len(loader)
    return lls_total


def mini_batch_em_epoch(num_epochs, pc, optimizer, scheduler, train_loader, test_loader, device):
    for epoch in range(num_epochs):
        t0 = time.time()
        train_ll = 0.0
        for batch in train_loader:
            x = batch[0].to(device)

            optimizer.zero_grad()

            lls = pc(x)
            lls.mean().backward()

            train_ll += lls.mean().detach().cpu().numpy().item()

            optimizer.step()
            scheduler.step()

        train_ll /= len(train_loader)

        t1 = time.time()
        test_ll = evaluate(pc, loader=test_loader)
        t2 = time.time()

        print(f"[Epoch {epoch}/{num_epochs}][train LL: {train_ll:.2f}; test LL: {test_ll:.2f}].....[train forward+backward+step {t1-t0:.2f}; test forward {t2-t1:.2f}] ")


def full_batch_em_epoch(pc, train_loader, test_loader, device):
    with torch.no_grad():
        t0 = time.time()
        train_ll = 0.0
        for batch in train_loader:
            x = batch[0].to(device)

            lls = pc(x)
            pc.backward(x, flows_memory = 1.0)

            train_ll += lls.mean().detach().cpu().numpy().item()

        pc.mini_batch_em(step_size = 1.0, pseudocount = 0.1)

        train_ll /= len(train_loader)

        t1 = time.time()
        test_ll = evaluate(pc, loader=test_loader)
        t2 = time.time()
        print(f"[train LL: {train_ll:.2f}; test LL: {test_ll:.2f}].....[train forward+backward+step {t1-t0:.2f}; test forward {t2-t1:.2f}] ")

def load_circuit(filename, verbose=False, device=None):
    t0 = time.time()
    if verbose:
        print(f"Loading circuit....{filename}.....", end="")
    pc = juice.model.ProbCircuit.load(filename)
    if device is not None:
        print(f"...into device {device}...", end="")
        pc.to(device)
    t1 = time.time()
    if verbose:
        print(f"Took {t1-t0:.2f} (s)")
        print("pc params size", pc.params.size())
        print("pc num nodes ", pc.num_nodes)

    return pc

def save_circuit(pc, filename, verbose=False):
    if verbose:
        print(f"Saving pc into {filename}.....", end="")
    t0_save = time.time()
    torch.save(pc, filename)
    t1_save = time.time()
    if verbose:
        print(f"took {t1_save - t0_save:.2f} (s)")
    

def main(args):
    torch.cuda.set_device(args.cuda)
    device = torch.device(f"cuda:{args.cuda}")
    filename = f"{args.output_dir}/{args.dataset}_{args.num_latents}.torch"
    
    if args.dataset == "mnist":
        train_dataset = torchvision.datasets.MNIST(root = "./examples/data", train = True, download = True)
        test_dataset = torchvision.datasets.MNIST(root = "./examples/data", train = False, download = True)
    elif args.dataset == "fashion":
        train_dataset = torchvision.datasets.FashionMNIST(root = "./examples/data", train = True, download = True)
        test_dataset = torchvision.datasets.FashionMNIST(root = "./examples/data", train = False, download = True)
    else:
        raise(f"Dataset {args.dataset} not supported.")
     
    train_data = train_dataset.data.reshape(60000, 28*28)
    test_data = test_dataset.data.reshape(10000, 28*28)

    num_features = train_data.size(1)

    train_loader = DataLoader(
        dataset = TensorDataset(train_data),
        batch_size = args.batch_size,
        shuffle = True,
        drop_last = True
    )
    test_loader = DataLoader(
        dataset = TensorDataset(test_data),
        batch_size = args.batch_size,
        shuffle = False,
        drop_last = True
    )

    if args.mode == "train":
        print("===========================Train===============================")
        if args.input_circuit is None:
            pc = juice.structures.HCLT(train_data.float().to(device), num_bins = 32, 
                                                                        sigma = 0.5 / 32, 
                                                                        num_latents = args.num_latents, 
                                                                        chunk_size = 32)
            pc.to(device)
        else:
            pc = load_circuit(args.input_circuit, verbose=True, device=device)
        
        optimizer = juice.optim.CircuitOptimizer(pc, lr = 0.1, pseudocount = 0.1)
        scheduler = juice.optim.CircuitScheduler(optimizer, method = "multi_linear", 
                                                            lrs = [0.9, 0.1, 0.05], 
                                                            milestone_steps = [0, len(train_loader) * 100, len(train_loader) * 350])

        mini_batch_em_epoch(350, pc, optimizer, scheduler, train_loader, test_loader, device)
        full_batch_em_epoch(pc, train_loader, test_loader, device)
        save_circuit(pc, filename, verbose=True)


    elif args.mode == "load":
        print("===========================LOAD===============================")
        pc = load_circuit(filename, verbose=True, device=device)

        t_compile = time.time()
        test_ll = evaluate(pc, loader=test_loader) # force compilation

        t0 = time.time()
        train_ll = evaluate(pc, loader=train_loader)
        t1 = time.time()
        test_ll = evaluate(pc, loader=test_loader)
        t2 = time.time()

        train_bpd = -train_ll / (num_features * np.log(2))
        test_bpd = -test_ll / (num_features * np.log(2))

        print(f"Compilation+test took {t0-t_compile:.2f} (s); train_ll {t1-t0:.2f} (s); test_ll {t2-t1:.2f} (s)")
        print(f"train_ll: {train_ll:.2f}, test_ll: {test_ll:.2f}")
        print(f"train_bpd: {train_bpd:.2f}, test_bpd: {test_bpd:.2f}")

    elif args.mode == "miss":
        print("===========================MISS===============================")    
        print(f"Loading {filename} into {device}.......")
        pc = load_circuit(filename, verbose=True, device=device)
    
        # test_miss_mask = torch.zeros(test_data.size(), dtype=torch.bool)
        # test_miss_mask[1:5000, 0:392] = 1 # for first half of images make first half missing
        # test_miss_mask[5000:, 392:] = 1   # for second half of images make second half missing
        test_miss_mask = torch.rand(test_data.size()) < 0.5

        test_loader_miss = DataLoader(
            dataset = TensorDataset(test_data, test_miss_mask),
            batch_size = args.batch_size,
            shuffle = False,
            drop_last = True
        )
        t_compile = time.time()
        test_ll = evaluate(pc, loader=test_loader) 
        test_ll_miss = evaluate_miss(pc, loader=test_loader_miss) 

        t0 = time.time()
        train_ll = evaluate(pc, loader=train_loader)
        t1 = time.time()
        test_ll = evaluate(pc, loader=test_loader) 
        t2 = time.time()
        test_ll_miss = evaluate_miss(pc, loader=test_loader_miss) 
        t3 = time.time()

        train_bpd = -train_ll / (num_features * np.log(2))
        test_bpd = -test_ll / (num_features * np.log(2))
        test_miss_bpd = -test_ll_miss / (num_features * np.log(2))

        print(f"train_ll: {train_ll:.2f}, train_bpd: {train_bpd:.2f}; time = {t1-t0:.2f} (s)")
        print(f"test_ll: {test_ll:.2f}, test_bpd: {test_bpd:.2f}; time = {t2-t1:.2f} (s)")
        print(f"test_miss_ll: {test_ll_miss:.2f}, test_miss_bpd: {test_miss_bpd:.2f}; time = {t3-t2:.2f} (s)")
    elif args.mode == "alphas":
        print("===========================ALPHAS===============================")
        pc = load_circuit(filename, verbose=True, device=device)

        alphas = 0.99 * torch.ones((args.batch_size, 28*28), device=device)
        test_ll = evaluate(pc, loader=test_loader) 
        train_ll = evaluate(pc, loader=train_loader)

        t0 = time.time()
        train_ll_alpha = evaluate(pc, loader=train_loader, alphas=alphas)
        t1 = time.time()
        test_ll_alpha = evaluate(pc, loader=test_loader, alphas=alphas)
        t2 = time.time()
        
        print(f"train_ll: {train_ll:.2f}, test_ll: {test_ll:.2f}")
        print(f"train_ll_alpha: {train_ll_alpha:.2f}, test_ll_alpha: {test_ll_alpha:.2f}")
        print(f"train {t1-t0:.2f} (s); test {t2-t1:.2f} (s)")

    elif args.mode == "sample":
        print("===========================SAMPLE===============================")
        pc = load_circuit(filename, verbose=True, device=device)
        pc.to(device)

        t0_sample = time.time()

        for batch_id, batch in enumerate(train_loader):
            x = batch[0].to(device)                                                 # (B, num_vars)
            miss_mask = torch.zeros(x.size(), dtype=torch.bool, device=device)      # (B, num_vars)
            
            # Left Side of Pixels Missing
            for row in range(28):
                miss_mask[:, row*28:row*28+14] = 1 

            # 1. Run Forward Pass
            lls = pc(x, missing_mask=miss_mask)

            # 2. Sample (for each item in batch returns a sample from p(. | x^o))
            samples = pc.sample(x, miss_mask)                                       # (B, num_vars)
 
            if batch_id < 2:
                # Plot first 8 Samples
                plot_count = 8
                print("Saving Samples as images to file")
                plt.figure()
                f, axarr = plt.subplots(3, plot_count, figsize=(28, 10))
                plt.gray()
                for i in range(plot_count):
                    axarr[0][i].imshow(x[i, :].reshape(28,28).cpu().numpy().astype(np.uint8))
                    axarr[1][i].imshow(255*miss_mask[i, :].reshape(28,28).cpu().numpy().astype(np.uint8))
                    axarr[2][i].imshow(samples[i, :].reshape(28,28).cpu().numpy().astype(np.uint8))
                plt.savefig(f"examples/1_pc_training/samples{batch_id}_test.png")  
        t1_sample = time.time()   
        print(f"Samples took {t1_sample - t0_sample:.2f} (s)")
    

    print(f"Memory allocated: {torch.cuda.memory_allocated(device) / 1024 / 1024 / 1024:.1f}GB")
    print(f"Memory reserved: {torch.cuda.memory_reserved(device) / 1024 / 1024 / 1024:.1f}GB")
    print(f"Max memory reserved: {torch.cuda.max_memory_reserved(device) / 1024 / 1024 / 1024:.1f}GB")


if __name__ == "__main__":
    args = process_args()
    print(args)
    main(args)