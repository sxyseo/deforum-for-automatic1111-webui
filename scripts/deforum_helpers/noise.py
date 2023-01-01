import torch
import math

# 2D Perlin noise in PyTorch https://gist.github.com/vadimkantorov/ac1b097753f217c5c11bc2ff396e0a57
def rand_perlin_2d(shape, res, fade = lambda t: 6*t**5 - 15*t**4 + 10*t**3):
    delta = (res[0] / shape[0], res[1] / shape[1])
    d = (shape[0] // res[0], shape[1] // res[1])
    
    grid = torch.stack(torch.meshgrid(torch.arange(0, res[0], delta[0]), torch.arange(0, res[1], delta[1]), indexing='ij'), dim = -1) % 1
    angles = 2*math.pi*torch.rand(res[0]+1, res[1]+1)
    gradients = torch.stack((torch.cos(angles), torch.sin(angles)), dim = -1)
    
    tile_grads = lambda slice1, slice2: gradients[slice1[0]:slice1[1], slice2[0]:slice2[1]].repeat_interleave(d[0], 0).repeat_interleave(d[1], 1)
    dot = lambda grad, shift: (torch.stack((grid[:shape[0],:shape[1],0] + shift[0], grid[:shape[0],:shape[1], 1] + shift[1]  ), dim = -1) * grad[:shape[0], :shape[1]]).sum(dim = -1)
    
    n00 = dot(tile_grads([0, -1], [0, -1]), [0,  0])
    n10 = dot(tile_grads([1, None], [0, -1]), [-1, 0])
    n01 = dot(tile_grads([0, -1],[1, None]), [0, -1])
    n11 = dot(tile_grads([1, None], [1, None]), [-1,-1])
    t = fade(grid[:shape[0], :shape[1]])
    return math.sqrt(2) * torch.lerp(torch.lerp(n00, n10, t[..., 0]), torch.lerp(n01, n11, t[..., 0]), t[..., 1])

def rand_perlin_2d_octaves(shape, res, octaves=1, persistence=0.5):
    noise = torch.zeros(shape)
    frequency = 1
    amplitude = 1
    for _ in range(int(octaves)):
        noise += amplitude * rand_perlin_2d(shape, (frequency*res[0], frequency*res[1]))
        frequency *= 2
        amplitude *= persistence
    return noise

#MASKARGSEXPANSION 
#Add option to remove noise in relation to masking so that areas which are masked receive less noise
def add_noise(sample: torch.Tensor, noise_amt: float, seed: int, noise_type: str, noise_args) -> torch.Tensor:
    seed_store = torch.seed()
    torch.manual_seed(seed) # Reproducibility
    noise = torch.randn(sample.shape, device=sample.device) # White noise
    if noise_type == 'perlin':
        # rand_perlin_2d_octaves is between -1 and 1, so we need to shift it to be between 0 and 1
        # print(sample.shape)
        sample2dshape = (sample.shape[2], sample.shape[3])
        noise = noise * ((rand_perlin_2d_octaves(sample2dshape, (int(noise_args[0]), int(noise_args[1])), octaves=noise_args[2], persistence=noise_args[3]) + torch.ones(sample2dshape)) / 2)
    sample = sample + noise * noise_amt
    torch.manual_seed(seed_store)
    return sample
