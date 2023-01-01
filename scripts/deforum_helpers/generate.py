import torch
import PIL #HOTFIXISSUE#33 needed for instruction to generate negative mask. 
from PIL import Image, ImageOps
import requests
import numpy as np
import torchvision.transforms.functional as TF
from pytorch_lightning import seed_everything
import os
from ldm.models.diffusion.plms import PLMSSampler
from ldm.models.diffusion.ddim import DDIMSampler
from k_diffusion.external import CompVisDenoiser
from torch import autocast
from contextlib import nullcontext
from einops import rearrange

from .prompt import get_uc_and_c, parse_weight
from .k_samplers import sampler_fn
from scipy.ndimage import gaussian_filter

from .callback import SamplerCallback

#Webui
import cv2
from .animation import sample_from_cv2, sample_to_cv2
from modules import processing
from modules.shared import opts, sd_model
from modules.processing import process_images, StableDiffusionProcessingTxt2Img
import logging

import math, json, itertools

def load_img(path, shape, use_alpha_as_mask=False):
    # use_alpha_as_mask: Read the alpha channel of the image as the mask image
    if path.startswith('http://') or path.startswith('https://'):
        image = Image.open(requests.get(path, stream=True).raw)
    else:
        image = Image.open(path)

    if use_alpha_as_mask:
        image = image.convert('RGBA')
    else:
        image = image.convert('RGB')

    image = image.resize(shape, resample=Image.LANCZOS)

    mask_image = None
    if use_alpha_as_mask:
        # Split alpha channel into a mask_image
        red, green, blue, alpha = Image.Image.split(image)
        mask_image = alpha.convert('L')
        image = image.convert('RGB')

    return image, mask_image #PIL image for auto's pipeline

def load_mask_latent(mask_input, shape):
    # mask_input (str or PIL Image.Image): Path to the mask image or a PIL Image object
    # shape (list-like len(4)): shape of the image to match, usually latent_image.shape
    
    if isinstance(mask_input, str): # mask input is probably a file name
        if mask_input.startswith('http://') or mask_input.startswith('https://'):
            mask_image = Image.open(requests.get(mask_input, stream=True).raw).convert('RGBA')
        else:
            mask_image = Image.open(mask_input).convert('RGBA')
    elif isinstance(mask_input, Image.Image):
        mask_image = mask_input
    else:
        raise Exception("mask_input must be a PIL image or a file name")

    mask_w_h = (shape[-1], shape[-2])
    mask = mask_image.resize(mask_w_h, resample=Image.LANCZOS)
    mask = mask.convert("L")
    return mask

def prepare_mask(mask_input, mask_shape, mask_brightness_adjust=1.0, mask_contrast_adjust=1.0, invert_mask=False):
    # mask_input (str or PIL Image.Image): Path to the mask image or a PIL Image object
    # shape (list-like len(4)): shape of the image to match, usually latent_image.shape
    # mask_brightness_adjust (non-negative float): amount to adjust brightness of the iamge, 
    #     0 is black, 1 is no adjustment, >1 is brighter
    # mask_contrast_adjust (non-negative float): amount to adjust contrast of the image, 
    #     0 is a flat grey image, 1 is no adjustment, >1 is more contrast
    
    mask = load_mask_latent(mask_input, mask_shape)

    # Mask brightness/contrast adjustments
    if mask_brightness_adjust != 1:
        mask = TF.adjust_brightness(mask, mask_brightness_adjust)
    if mask_contrast_adjust != 1:
        mask = TF.adjust_contrast(mask, mask_contrast_adjust)

    if invert_mask:
        mask = PIL.ImageOps.invert(mask)
    
    return mask

def isJson(myjson):
    try:
        json.loads(myjson)
    except ValueError as e:
        return False
    return True

def generate(args, anim_args, root, frame = 0, return_sample=False):
    import re
    assert args.prompt is not None
    
    # Evaluate prompt math!
    
    math_parser = re.compile("""
            (?P<weight>(
            `[\S\s]*?`# a math function wrapped in `-characters
            ))
            """, re.VERBOSE)
    
    parsed_prompt = re.sub(math_parser, lambda m: str(parse_weight(m, frame)), args.prompt)
    
    # Setup the pipeline
    p = root.p
    
    os.makedirs(args.outdir, exist_ok=True)
    p.batch_size = args.n_samples
    p.width = args.W
    p.height = args.H
    p.seed = args.seed
    p.subseed=args.subseed
    p.subseed_strength=args.subseed_strength
    p.do_not_save_samples = not args.save_sample_per_step
    p.do_not_save_grid = not args.make_grid
    p.sd_model=sd_model
    p.sampler_name = args.sampler
    p.mask_blur = args.mask_overlay_blur
    p.extra_generation_params["Mask blur"] = args.mask_overlay_blur
    p.n_iter = 1
    p.steps = args.steps
    p.denoising_strength = 1 - args.strength
    p.cfg_scale = args.scale
    p.seed_enable_extras = args.seed_enable_extras

    # FIXME better color corrections as match histograms doesn't seem to be fully working
    if root.color_corrections is not None:
        p.color_corrections = root.color_corrections
    p.outpath_samples = root.outpath_samples
    p.outpath_grids = root.outpath_samples
    
    prompt_split = parsed_prompt.split("--neg")
    if len(prompt_split) > 1:
        p.prompt, p.negative_prompt = parsed_prompt.split("--neg") #TODO: add --neg to vanilla Deforum for compat
        print(f'Positive prompt:{p.prompt}')
        print(f'Negative prompt:{p.negative_prompt}')
    else:
        p.prompt = prompt_split[0]
        print(f'Positive prompt:{p.prompt}')
        p.negative_prompt = ""
    
    if not args.use_init and args.strength > 0 and args.strength_0_no_init:
        print("\nNo init image, but strength > 0. Strength has been auto set to 0, since use_init is False.")
        print("If you want to force strength > 0 with no init, please set strength_0_no_init to False.\n")
        args.strength = 0
    
    processed = None
    mask_image = None
    init_image = None
    image_init0 = None

    # some setup variables that should be broken out later
    if args.init_image != None and isJson(args.init_image):
        tweeningFrames = 20
        blendFactor = .07
        colorCorrectionFactor = .075
        jsonImages = json.loads(args.init_image)
        framesToImageSwapOn = list(map(int, list(jsonImages.keys())))
        
        # find which image to show
        frameToChoose = 0
        for swappingFrame in framesToImageSwapOn[1:]:
            frameToChoose += (frame >= int(swappingFrame))
        
        #find which frame to do our swapping on for tweening
        skipFrame = 25
        for fs, fe in itertools.pairwise(framesToImageSwapOn):
            if fs <= frame <= fe:
                skipFrame = fe - fs

        if frame % skipFrame <= tweeningFrames: # number of tweening frames
            blendFactor = .35 - .25*math.cos((frame % tweeningFrames) / (tweeningFrames / 2))
        
        print(f"\nframe: {frame} - blend factor: {blendFactor:.5f} - strength:{args.strength:.5f} - denoising: {p.denoising_strength:.5f} - skipFrame: {skipFrame}\n")
        init_image2, _ = load_img(list(jsonImages.values())[frameToChoose],
                                shape=(args.W, args.H),
                                use_alpha_as_mask=args.use_alpha_as_mask)
        image_init0 = list(jsonImages.values())[0]
    else: # they passed in a single init image
        image_init0 = args.init_image


    if args.init_sample is not None:
        open_cv_image = sample_to_cv2(args.init_sample)
        img = cv2.cvtColor(open_cv_image, cv2.COLOR_BGR2RGB)
        init_image = Image.fromarray(img)
        image_init0 = Image.fromarray(img)
        if args.init_image is not None and isJson(args.init_image):
            init_image = Image.blend(init_image, init_image2, blendFactor)
            correction_colors = Image.blend(init_image, init_image2, colorCorrectionFactor)
            p.color_corrections = [processing.setup_color_correction(correction_colors)]

    # this is the first pass
    elif args.use_init and ((args.init_image != None and args.init_image != '')):
        init_image, mask_image = load_img(image_init0, # initial init image
                                          shape=(args.W, args.H),  
                                          use_alpha_as_mask=args.use_alpha_as_mask)
    else:
        print(f"Not using an init image (doing pure txt2img) - seed:{p.seed}; subseed:{p.subseed}; subseed_strength:{p.subseed_strength}; cfg_scale:{p.cfg_scale}; steps:{p.steps}")
        p_txt = StableDiffusionProcessingTxt2Img(
                sd_model=sd_model,
                outpath_samples=p.outpath_samples,
                outpath_grids=p.outpath_samples,
                prompt=p.prompt,
                styles=p.styles,
                negative_prompt=p.negative_prompt,
                seed=p.seed,
                subseed=p.subseed,
                subseed_strength=p.subseed_strength,
                seed_resize_from_h=p.seed_resize_from_h,
                seed_resize_from_w=p.seed_resize_from_w,
                seed_enable_extras=p.seed_enable_extras,
                sampler_name=p.sampler_name,
                batch_size=p.batch_size,
                n_iter=p.n_iter,
                steps=p.steps,
                cfg_scale=p.cfg_scale,
                width=p.width,
                height=p.height,
                restore_faces=p.restore_faces,
                tiling=p.tiling,
                enable_hr=None,
                denoising_strength=None,
            )
        processed = processing.process_images(p_txt)
    
    if processed is None:
        # Mask functions
        if args.use_mask:
            assert args.mask_file is not None or mask_image is not None, "use_mask==True: An mask image is required for a mask. Please enter a mask_file or use an init image with an alpha channel"
            assert args.use_init, "use_mask==True: use_init is required for a mask"
            mask = prepare_mask(args.mask_file if mask_image is None else mask_image, 
                                (args.W, args.H), 
                                args.mask_contrast_adjust, 
                                args.mask_brightness_adjust, 
                                args.invert_mask)
                                
            p.inpainting_fill = args.fill # need to come up with better name. 
            p.inpaint_full_res= args.full_res_mask 
            p.inpaint_full_res_padding = args.full_res_mask_padding 

            #if (torch.all(mask == 0) or torch.all(mask == 1)) and args.use_alpha_as_mask:
            #    raise Warning("use_alpha_as_mask==True: Using the alpha channel from the init image as a mask, but the alpha channel is blank.")
            
            #mask = repeat(mask, '1 ... -> b ...', b=batch_size)
        else:
            mask = None

        assert not ( (args.use_mask and args.overlay_mask) and (args.init_sample is None and init_image is None)), "Need an init image when use_mask == True and overlay_mask == True"
        
        p.init_images = [init_image]
        p.image_mask = mask

        print(f"seed={p.seed}; subseed={p.subseed}; subseed_strength={p.subseed_strength}; denoising_strength={p.denoising_strength}; steps={p.steps}; cfg_scale={p.cfg_scale}")
        processed = processing.process_images(p)
        p.sd_model=sd_model
    
    if root.initial_info == None:
        root.initial_seed = processed.seed
        root.initial_info = processed.info
    
    if root.first_frame == None:
        root.first_frame = processed.images[0]
        ### TODO: put the correct arg here.
        if anim_args.histogram_matching:
            root.color_corrections = [processing.setup_color_correction(root.first_frame)]
    
    if return_sample:
        pil_image = processed.images[0].convert('RGB') 
        open_cv_image = np.array(pil_image) 
        # Convert RGB to BGR 
        open_cv_image = open_cv_image[:, :, ::-1].copy() 
        image = sample_from_cv2(open_cv_image)
        results = [image, processed.images[0]]
    else:
        results = [processed.images[0]]
    
    return results
