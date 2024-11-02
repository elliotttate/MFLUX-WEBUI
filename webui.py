import tempfile
import traceback
import gradio as gr
import time
import os
from pathlib import Path
from huggingface_hub import hf_hub_download, HfApi
import ollama
import json
from functools import partial
import mlx.core as mx
import gc
from functools import lru_cache
import requests
from bs4 import BeautifulSoup
import re
import subprocess
from mflux.config.config import Config, ConfigControlnet
from mflux.flux.flux import Flux1
from mflux.controlnet.flux_controlnet import Flux1Controlnet
from tqdm import tqdm
from huggingface_hub import HfApi, HfFolder
from PIL import Image
from mflux.ui.cli.parsers import CommandLineParser
import base64
from io import BytesIO
import numpy as np
import random

LORA_DIR = os.path.join(os.path.dirname(__file__), "lora")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)       

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(MODELS_DIR, exist_ok=True)

flux_cache = {}

def get_lora_choices():
    return [name for name, _ in get_available_lora_files()]

class CustomModelConfig:
    def __init__(self, model_name, alias, num_train_steps, max_sequence_length):
        self.model_name = model_name
        self.alias = alias
        self.num_train_steps = num_train_steps
        self.max_sequence_length = max_sequence_length

    @staticmethod
    def from_alias(alias):
        return get_custom_model_config(alias)

MODELS = {
    "dev": CustomModelConfig("AITRADER/MFLUXUI.1-dev", "dev", 1000, 512),
    "schnell": CustomModelConfig("AITRADER/MFLUXUI.1-schnell", "schnell", 1000, 256),
    "dev-8-bit": CustomModelConfig("AITRADER/MFLUX.1-dev-8-bit", "dev-8-bit", 1000, 512),
    "dev-4-bit": CustomModelConfig("AITRADER/MFLUX.1-dev-4-bit", "dev-4-bit", 1000, 512),
    "schnell-8-bit": CustomModelConfig("AITRADER/MFLUX.1-schnell-8-bit", "schnell-8-bit", 1000, 256),
    "schnell-4-bit": CustomModelConfig("AITRADER/MFLUX.1-schnell-4-bit", "schnell-4-bit", 1000, 256),
}

def get_custom_model_config(model_alias):
    config = MODELS.get(model_alias)
    if config is None:
        raise ValueError(f"Invalid model alias: {model_alias}. Available aliases are: {', '.join(MODELS.keys())}")
    return config

from huggingface_hub import snapshot_download

def download_and_save_model(hf_model_name, alias, num_train_steps, max_sequence_length):
    try:
        local_dir = os.path.join(MODELS_DIR, alias)
        snapshot_download(repo_id=hf_model_name, local_dir=local_dir, local_dir_use_symlinks=False)
        
        new_config = CustomModelConfig(hf_model_name, alias, num_train_steps, max_sequence_length)
        get_custom_model_config.__globals__['models'][alias] = new_config
        
        return f"Model {hf_model_name} successfully downloaded and saved as {alias}"
    except Exception as e:
        return f"Error downloading model: {str(e)}"

flux_cache = {}

def download_and_save_model(hf_model_name, alias, num_train_steps, max_sequence_length):
    try:
        model_dir = os.path.join(MODELS_DIR, alias)
        os.makedirs(model_dir, exist_ok=True)

        downloaded_files = snapshot_download(repo_id=hf_model_name, local_dir=model_dir)
        
        new_config = CustomModelConfig(hf_model_name, alias, num_train_steps, max_sequence_length)
        get_custom_model_config.__globals__['models'][alias] = new_config
        
        return f"Model {hf_model_name} successfully downloaded and saved as {alias} in {model_dir}. Downloaded files: {len(downloaded_files)}"
    except Exception as e:
        return f"Error: {str(e)}"

def get_or_create_flux(model, quantize, path, lora_paths_tuple, lora_scales_tuple, is_controlnet=False):
    lora_paths = list(lora_paths_tuple) if lora_paths_tuple else None
    lora_scales = list(lora_scales_tuple) if lora_scales_tuple else None
    
    FluxClass = Flux1Controlnet if is_controlnet else Flux1
    
    base_model = model.replace("-8-bit", "").replace("-4-bit", "")
    
    try:
        custom_config = get_custom_model_config(base_model)
        if base_model in ["dev", "schnell", "dev-8-bit", "dev-4-bit", "schnell-8-bit", "schnell-4-bit"]:
            model_path = None
        else:
            model_path = os.path.join(MODELS_DIR, base_model)
    except ValueError:
        custom_config = CustomModelConfig(base_model, base_model, 1000, 512)
        model_path = os.path.join(MODELS_DIR, base_model)
    
    if "-8-bit" in model:
        quantize = 8
    elif "-4-bit" in model:
        quantize = 4
    
    flux = FluxClass(
        model_config=custom_config,
        quantize=quantize,
        local_path=model_path,
        lora_paths=lora_paths,
        lora_scales=lora_scales,
    )
    
    return flux

def get_available_lora_files():
    lora_files = []
    for root, dirs, files in os.walk(LORA_DIR):
        for file in files:
            if file.endswith(".safetensors"):
                display_name = os.path.splitext(file)[0]
                lora_files.append((display_name, os.path.join(root, file)))
    lora_files.sort(key=lambda x: x[0].lower())
    return lora_files

def get_available_models():
    standard_models = ["schnell-4-bit", "schnell-8-bit", "dev-4-bit", "dev-8-bit", "schnell", "dev"]
    custom_models = [f.name for f in Path(MODELS_DIR).iterdir() if f.is_dir()]
    return standard_models + custom_models

def ensure_llama_model(model_name):
    try:
        ollama.pull(model_name)
        return True
    except Exception:
        return False

def load_ollama_settings():
    try:
        with open('ollama_settings.json', 'r') as f:
            settings = json.load(f)
    except FileNotFoundError:
        _, default_model = get_available_ollama_models()
        settings = {'model': default_model}
    
    settings['system_prompt'] = read_system_prompt()
    return settings

def create_ollama_settings(include_prompt_selector=False, prompt_type="standard"):
    settings = load_ollama_settings()
    available_models, _ = get_available_ollama_models()
    
    ollama_model = gr.Dropdown(
        choices=available_models,
        value=settings['model'],
        label="Ollama Model"
    )
    
    if prompt_type == "controlnet":
        prompts = {
            "Standard": settings['system_prompt'],
            "ControlNet Prompt": read_controlnet_prompt()
        }
    elif prompt_type == "i2i":
        prompts = {
            "Standard": settings['system_prompt'],
            "Image-to-Image Prompt": read_i2i_prompt()
        }
    else:
        prompts = {"Standard": settings['system_prompt']}
    
    if include_prompt_selector:
        prompt_choices = list(prompts.keys())
        prompt_selector = gr.Dropdown(
            choices=prompt_choices,
            value=prompt_choices[0],
            label="Select System Prompt"
        )
        system_prompt = gr.Textbox(
            label="System Prompt", 
            lines=10,
            value=prompts[prompt_choices[0]]
        )
        
        def update_system_prompt(selected_prompt):
            return prompts[selected_prompt]
        
        prompt_selector.change(
            fn=update_system_prompt,
            inputs=[prompt_selector],
            outputs=[system_prompt]
        )
    else:
        system_prompt = gr.Textbox(
            label="System Prompt", 
            lines=10,
            value=list(prompts.values())[0]
        )
        prompt_selector = None

    save_button = gr.Button("Save Ollama Settings")
    
    if include_prompt_selector:
        return [ollama_model, system_prompt, save_button, prompt_selector]
    else:
        return [ollama_model, system_prompt, save_button]

def save_settings(model, prompt, selected_prompt=None):
    settings = {
        'model': model,
        'system_prompt': prompt
    }
    
    with open('ollama_settings.json', 'w') as f:
        json.dump(settings, f)
    
    gr.Info("Settings saved!")
    return gr.update(open=False)

def enhance_prompt(prompt, ollama_model, system_prompt, control_image=None):
    try:
        if not prompt or not ollama_model:
            return prompt

        system_prompt = system_prompt.replace("{user_prompt}", prompt)
        
        system_prompt = system_prompt.replace('"{user_prompt}"', f'"{prompt}"')

        print("\n=== Debug Prompts ===")
        print(f"User UI prompt: {prompt}")
        print(f"System prompt from file: {system_prompt}")
        print(f"Model being used: {ollama_model}")
        print(f"Has control image: {control_image is not None}")

        if control_image is not None:
            buffered = BytesIO()
            control_image.save(buffered, format="PNG")
            img_str = base64.b64encode(buffered.getvalue()).decode()
            
            print("\n=== Sending to Ollama ===")
            print(f"Final prompt being sent: {prompt}")
            print(f"Final system prompt being sent: {system_prompt}")
            
            response = ollama.generate(
                model=ollama_model,
                prompt=prompt,
                system=system_prompt,
                images=[img_str]
            )
        else:
            response = ollama.generate(
                model=ollama_model,
                prompt=prompt,
                system=system_prompt
            )

        result = response['response'] if isinstance(response, dict) else response.response
        print(f"\nOllama response: {result}")
        
        return result
    except Exception as e:
        print(f"Error enhancing prompt: {str(e)}")
        return prompt

def print_memory_usage(label):
    try:
        active_memory = mx.metal.get_active_memory() / 1e6
        peak_memory = mx.metal.get_peak_memory() / 1e6
        print(f"{label} - Active memory: {active_memory:.2f} MB, Peak memory: {peak_memory:.2f} MB")
    except AttributeError:
        print(f"{label} - Unable to get memory usage information")

def generate_image_gradio(
    prompt, model, seed, height, width, steps, guidance, lora_files, metadata, ollama_model, system_prompt
):
    print(f"\n--- Generating image (Advanced) ---")
    print(f"Model: {model}")
    print(f"Prompt: {prompt}")
    print(f"Adjusted Dimensions: {height}x{width}")
    print(f"Steps: {steps}")
    print(f"Guidance: {guidance}")
    print(f"LoRA files: {lora_files}")
    print_memory_usage("Before generation")

    start_time = time.time()

    try:

        valid_loras = process_lora_files(lora_files)
        lora_paths = valid_loras if valid_loras else None
        lora_scales = [1.0] * len(valid_loras) if valid_loras else None

        seed = None if seed == "" else int(seed)

        if not steps or steps.strip() == "":
            base_model = model.replace("-4-bit", "").replace("-8-bit", "")
            if "schnell" in base_model:
                steps = 4
            elif "dev" in base_model:
                steps = 20
            else:
                steps = 20
        else:
            steps = int(steps)

        flux = get_or_create_flux(model, None, None, lora_paths, lora_scales)

        print_memory_usage("After creating flux")

        timestamp = int(time.time())
        output_filename = f"generated_{timestamp}.png"
        output_path = os.path.join(OUTPUT_DIR, output_filename)

        image = flux.generate_image(
            seed=int(time.time()) if seed is None else seed,
            prompt=prompt,
            config=Config(
                num_inference_steps=steps,
                height=height,
                width=width,
                guidance=guidance,
            ),
        )

        image.save(output_path)

        print_memory_usage("After generating image")
        print_memory_usage("After saving image")

        del flux
        del image
        gc.collect()
        force_mlx_cleanup()

        end_time = time.time()
        generation_time = end_time - start_time
        print(f"Generation time: {generation_time:.2f} seconds")

        return output_path, output_filename, prompt

    except Exception as e:
        print(f"Error generating image: {str(e)}")
        traceback.print_exc()
        return None, None, prompt

    finally:
        force_mlx_cleanup()
        gc.collect()

def generate_image_controlnet_gradio(
    prompt,
    control_image,
    model,
    seed,
    height,
    width,
    steps,
    guidance,
    controlnet_strength,
    lora_files,
    metadata,
    save_canny,
    ollama_model,
    system_prompt
):
    print(f"\n--- Generating image (ControlNet) ---")
    print(f"Received parameters:")
    print(f"- prompt: {prompt}")
    print(f"- model: {model}")
    print(f"- seed: {seed}")
    print(f"- height: {height}")
    print(f"- width: {width}") 
    print(f"- steps: {steps}")
    print(f"- guidance: {guidance}")
    print(f"- controlnet_strength: {controlnet_strength}")
    print(f"- lora_files: {lora_files}")
    print(f"- save_canny: {save_canny}")

    print_memory_usage("Before generation")
    start_time = time.time()
    generated_image = None
    canny_image_to_return = None
    try:
        valid_loras = process_lora_files(lora_files)
        lora_paths = valid_loras if valid_loras else None
        lora_scales = [1.0] * len(valid_loras) if valid_loras else None

        seed = None if seed == "" else int(seed)
        steps = None if steps == "" else int(steps)
        if steps is None:
            steps = 4 if "schnell" in model else 14

        flux = get_or_create_flux(
            model, 
            None, 
            None,
            lora_paths,
            lora_scales,
            is_controlnet=True
        )

        timestamp = int(time.time())
        control_image_path = os.path.join(OUTPUT_DIR, f"control_image_{timestamp}.png")
        output_path = os.path.join(OUTPUT_DIR, f"generated_controlnet_{timestamp}.png")
        control_image.save(control_image_path)

        generated_image = flux.generate_image(
            seed=int(time.time()) if seed is None else seed,
            prompt=prompt,
            controlnet_image_path=control_image_path,
            config=ConfigControlnet(
                num_inference_steps=steps,
                height=height,
                width=width,
                guidance=guidance,
                controlnet_strength=controlnet_strength,
            ),
            output=output_path,
            controlnet_save_canny=save_canny
        )

        canny_image = None
        if save_canny:
            canny_path = output_path.replace('.png', '_controlnet_canny.png')
            if os.path.exists(canny_path):
                canny_image = Image.open(canny_path)
                print(f"Canny image geladen van {canny_path}")
            else:
                print(f"Canny image niet gevonden op {canny_path}")

        print_memory_usage("After generating image")

        generated_image.image.save(output_path)

        print(f"Generation completed in {time.time() - start_time:.2f}s")
        return generated_image.image, "Generation successful!", prompt, canny_image

    except Exception as e:
        print(f"\nError in ControlNet generation: {str(e)}")
        print(f"Full traceback:")
        traceback.print_exc()
        return None, f"Error: {str(e)}", prompt, None

    finally:
        if 'control_image_path' in locals() and os.path.exists(control_image_path):
            os.remove(control_image_path)
        if flux:
            del flux
        if generated_image:
            del generated_image
        gc.collect()
        force_mlx_cleanup()

def process_lora_files(selected_loras):
    if not selected_loras:
        return []
    lora_files = get_available_lora_files()
    if not lora_files:
        return []
    lora_dict = dict(lora_files)
    valid_loras = []
    for lora in selected_loras:
        if lora in lora_dict:
            valid_loras.append(lora_dict[lora])
    return valid_loras

def save_quantized_model_gradio(model, quantize):
    quantize = int(quantize)
    try:
        custom_config = get_custom_model_config(model)
    except ValueError:
        custom_config = CustomModelConfig(model, model, 1000, 512)
    
    model_path = os.path.join(MODELS_DIR, model)
    
    flux = Flux1(
        model_config=custom_config,
        quantize=quantize,
        local_path=model_path
    )

    save_path = os.path.join(MODELS_DIR, f"{model}-{quantize}-bit")
    flux.save_model(save_path)

    updated_models = get_updated_models()
    return (
        gr.update(choices=updated_models),
        gr.update(choices=updated_models),
        gr.update(choices=updated_models),
        gr.update(choices=[m for m in updated_models if not m.endswith("-4-bit") and not m.endswith("-8-bit")]),
        f"Model gekwantiseerd en opgeslagen als {save_path}"
    )

def simple_generate_image(prompt, model, image_format, lora_files, ollama_model, system_prompt):
    print(f"\n--- Generating image ---")
    print(f"Model: {model}")
    print(f"Prompt: {prompt}")
    print(f"Image Format: {image_format}")
    print(f"LoRA files: {lora_files}")
    print_memory_usage("Before generation")

    start_time = time.time()

    try:
        width, height = map(int, image_format.split('(')[1].split(')')[0].split('x'))

        valid_loras = process_lora_files(lora_files)
        lora_paths = valid_loras if valid_loras else None
        lora_scales = [1.0] * len(valid_loras) if valid_loras else None

        if "dev" in model:
            steps = 20
        else:
            steps = 4

        flux = get_or_create_flux(model, None, None, lora_paths, lora_scales)

        timestamp = int(time.time())
        output_filename = f"generated_simple_{timestamp}.png"
        output_path = os.path.join(OUTPUT_DIR, output_filename)

        image = flux.generate_image(
            seed=int(time.time()),
            prompt=prompt,
            config=Config(
                num_inference_steps=steps,
                height=height,
                width=width,
                guidance=7.5,
            ),
        )

        print_memory_usage("After generating image")
        
        image.image.save(output_path)

        print_memory_usage("After saving image")
        
        del flux
        del image
        
        gc.collect()
        
        force_mlx_cleanup()

        print_memory_usage("After cleanup")

        end_time = time.time()
        generation_time = end_time - start_time
        print(f"Generation time: {generation_time:.2f} seconds")

        return output_path, output_filename, prompt

    except Exception as e:
        print(f"Error in image generation: {str(e)}")
        return None, None, prompt
    finally:
        force_mlx_cleanup()
        gc.collect()

def get_available_ollama_models():
    try:
        models = ollama.list()
        available_models = [model['name'] for model in models['models']]
        return available_models, available_models[0] if available_models else None
    except Exception as e:
        print(f"Error fetching Ollama models: {e}")
        return [], None

def save_ollama_settings(model, system_prompt):
    with open('ollama_settings.json', 'w') as f:
        json.dump({'model': model}, f)
    
    with open('system_prompt.md', 'w') as f:
        f.write(system_prompt)

def read_system_prompt():
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        system_prompt_path = os.path.join(script_dir, 'system_prompt.md')
        with open(system_prompt_path, 'r') as file:
            return file.read()
    except FileNotFoundError:
        print("system_prompt.md not found")
        return ""

def read_controlnet_prompt():
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        prompt_path = os.path.join(script_dir, 'controlnet_prompt.md')
        with open(prompt_path, 'r') as file:
            return file.read()
    except FileNotFoundError:
        print("controlnet_prompt.md not found")
        return ""

def read_i2i_prompt():
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        prompt_path = os.path.join(script_dir, 'image_to_image_prompt.md')
        with open(prompt_path, 'r') as file:
            return file.read()
    except FileNotFoundError:
        print("image_to_image_prompt.md not found")
        return ""

def clear_flux_cache():
    global flux_cache
    
    flux_cache.clear()
    
    gc.collect()
    
    try:
        mx.metal.reset_peak_memory()
        
        mx.eval(mx.zeros(1))
        
        if hasattr(mx, 'clear_memory_pool'):
            mx.clear_memory_pool()
        
        if hasattr(mx, 'metal') and hasattr(mx.metal, 'device_reset'):
            mx.metal.device_reset()
        
    except AttributeError as e:
        print(f"Waarschuwing: Sommige MLX geheugenbeheerfuncties zijn niet beschikbaar: {e}")
    
    gc.collect()
    
    print_memory_usage("After clearing flux cache")

def force_mlx_cleanup():
    mx.eval(mx.zeros(1))
    
    if hasattr(mx.metal, 'clear_cache'):
        mx.metal.clear_cache()
    
    if hasattr(mx.metal, 'reset_peak_memory'):
        mx.metal.reset_peak_memory()

    gc.collect()

def update_guidance_visibility(model):
    return gr.update(visible="dev" in model)

def save_api_key(api_key, key_type="civitai"):
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = json.load(f)
    config[f"{key_type}_api_key"] = api_key
    with open(config_path, "w") as f:
        json.dump(config, f)
    return "API key saved successfully"

def load_api_key(key_type="civitai"):
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = json.load(f)
        return config.get(f"{key_type}_api_key", "")
    return ""

def download_lora_model(page_url, api_key):
    def slugify(value):
        value = str(value)
        value = re.sub('[^\w\s-]', '', value).strip().lower()
        value = re.sub('[-\s]+', '-', value)
        return value

    if not api_key:
        return gr.update(), gr.update(), gr.update(), "Error: API key is missing"

    try:
        print(f"Starting download process for URL: {page_url}")
        model_id_match = re.search(r'/models/(\d+)', page_url)
        if not model_id_match:
            return gr.update(), gr.update(), gr.update(), f"Error: Could not extract model ID from the URL: {page_url}"

        model_id = model_id_match.group(1)
        api_url = f"https://civitai.com/api/v1/models/{model_id}"
        
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        model_data = response.json()

        if not model_data.get('modelVersions'):
            return gr.update(), gr.update(), gr.update(), "Error: No model versions found"

        model_name = model_data.get('name', f"model_{model_id}")
        latest_version = model_data['modelVersions'][0]
        version_name = latest_version.get('name', 'unknown_version')

        model_name_slug = slugify(model_name)
        version_name_slug = slugify(version_name)

        filename = f"{model_name_slug}-{version_name_slug}.safetensors"
        file_path = os.path.join(LORA_DIR, filename)

        download_url = latest_version['downloadUrl']
        
        print(f"Download URL: {download_url}")

        download_response = requests.get(download_url, headers=headers, stream=True)
        download_response.raise_for_status()

        total_size = int(download_response.headers.get('content-length', 0))
        
        print(f"Saving to: {file_path}")
        print(f"Total size: {total_size} bytes")

        with open(file_path, 'wb') as file, tqdm(
            desc=filename,
            total=total_size,
            unit='iB',
            unit_scale=True,
            unit_divisor=1024,
        ) as progress_bar:
            for data in download_response.iter_content(chunk_size=8192):
                size = file.write(data)
                progress_bar.update(size)

        print(f"Download completed successfully: {filename}")
        
        updated_lora_files = get_updated_lora_files()
        return (
            gr.update(choices=updated_lora_files),
            gr.update(choices=updated_lora_files),
            gr.update(choices=updated_lora_files),
            f"Download completed successfully: {filename}"
        )

    except requests.exceptions.RequestException as e:
        error_message = f"Network error during download: {str(e)}"
        if hasattr(e, 'response') and e.response is not None:
            error_message += f"\nStatus code: {e.response.status_code}"
            error_message += f"\nResponse content: {e.response.text[:500]}..."
        print(f"Error: {error_message}")
        return gr.update(), gr.update(), gr.update(), error_message

    except Exception as e:
        error_message = f"Unexpected error during download: {str(e)}"
        print(f"Error: {error_message}")
        return gr.update(), gr.update(), gr.update(), error_message

def get_updated_lora_files():
    lora_files = []
    for root, dirs, files in os.walk(LORA_DIR):
        for file in files:
            if file.endswith(".safetensors") or file.endswith(".ckpt"):
                lora_files.append(file)
    return lora_files

def get_updated_models():
    predefined_models = ["schnell-4-bit", "dev-4-bit", "schnell-8-bit", "dev-8-bit", "schnell", "dev"]
    custom_models = [f.name for f in Path(MODELS_DIR).iterdir() if f.is_dir()]
    custom_models = [m for m in custom_models if m not in predefined_models]
    custom_models.sort(key=str.lower)
    all_models = predefined_models + custom_models
    return all_models

def download_and_save_model(hf_model_name, alias, num_train_steps, max_sequence_length, api_key):
    try:
        login_result = login_huggingface(api_key)
        if "Error" in login_result:
            return gr.update(), gr.update(), gr.update(), gr.update(), login_result

        model_dir = os.path.join(MODELS_DIR, alias)
        os.makedirs(model_dir, exist_ok=True)

        downloaded_files = snapshot_download(repo_id=hf_model_name, local_dir=model_dir, use_auth_token=api_key)
        
        new_config = CustomModelConfig(hf_model_name, alias, num_train_steps, max_sequence_length)
        get_custom_model_config.__globals__['models'][alias] = new_config
        
        print(f"Model {hf_model_name} successfully downloaded and saved as {alias}")
        
        updated_models = get_updated_models()
        return (
            gr.update(choices=updated_models),
            gr.update(choices=updated_models),
            gr.update(choices=updated_models),
            gr.update(choices=[m for m in updated_models if not m.endswith("-4-bit") and not m.endswith("-8-bit")]),
            f"Model {hf_model_name} successfully downloaded and saved as {alias}"
        )

    except Exception as e:
        error_message = f"Error downloading model: {str(e)}"
        print(f"Error: {error_message}")
        return gr.update(), gr.update(), gr.update(), gr.update(), error_message

def load_hf_api_key():
    try:
        with open('hf_api_key.json', 'r') as f:
            settings = json.load(f)
        return settings.get('api_key', '')
    except FileNotFoundError:
        return ''

def save_hf_api_key(api_key):
    return save_api_key(api_key, "huggingface")

def login_huggingface(api_key):
    try:
        api = HfApi()
        api.set_access_token(api_key)
        HfFolder.save_token(api_key)
        return "Successfully logged in to Hugging Face"
    except Exception as e:
        return f"Error logging in to Hugging Face: {str(e)}"

def download_lora_model_huggingface(model_name, hf_api_key):
    if not model_name:
        return gr.update(), gr.update(), gr.update(), "Error: Model name is missing"

    try:
        api = HfApi(token=hf_api_key if hf_api_key else None)

        if '/' in model_name:
            owner, repo_name = model_name.split('/', 1)
        else:
            repo_name = model_name

        files = api.list_repo_files(repo_id=model_name)

        safetensors_files = [f for f in files if f.endswith(".safetensors")]

        if not safetensors_files:
            return gr.update(), gr.update(), gr.update(), f"Error: No .safetensors files found in the repository '{model_name}'"

        downloaded_files = []
        for filename in safetensors_files:
            local_file_path = hf_hub_download(
                repo_id=model_name,
                filename=filename,
                local_dir=LORA_DIR,
                use_auth_token=hf_api_key,
                force_download=True
            )

            if repo_name in os.path.basename(filename):
                new_filename = os.path.basename(filename)
            else:
                new_filename = f"{repo_name}-{os.path.basename(filename)}"
            
            if not new_filename.endswith('.safetensors'):
                new_filename = f"{new_filename}.safetensors"

            new_file_path = os.path.join(LORA_DIR, new_filename)

            os.rename(local_file_path, new_file_path)
            downloaded_files.append(new_filename)

        print(f"Download completed: {', '.join(downloaded_files)}")
        updated_lora_files = get_updated_lora_files()
        return (
            gr.update(choices=updated_lora_files),
            gr.update(choices=updated_lora_files),
            gr.update(choices=updated_lora_files),
            f"Download completed: {', '.join(downloaded_files)}"
        )

    except Exception as e:
        error_message = f"Error downloading LoRA from HuggingFace: {str(e)}"
        print(f"Error: {error_message}")
        return gr.update(), gr.update(), gr.update(), error_message

def generate_image_i2i_gradio(
    prompt,
    init_image,
    init_image_strength,
    model,
    seed,
    width,
    height,
    steps,
    guidance,
    lora_files,
    lora_scale,
    metadata,
    ollama_model,
    system_prompt
):
    if init_image is None:
        return None, None, prompt

    if not isinstance(init_image, Image.Image):
        init_image = Image.fromarray(init_image)

    width = int(width - (width % 16))
    height = int(height - (height % 16))
    init_image = init_image.resize((width, height))

    if not steps or steps.strip() == "":
        base_model = model.replace("-4-bit", "").replace("-8-bit", "")
        steps = 4 if "schnell" in base_model else 20
    else:
        steps = int(steps)

    if init_image_strength is not None and init_image_strength < 1.0:
        num_inference_steps = int(steps / (1 - init_image_strength))
    else:
        num_inference_steps = steps

    num_inference_steps = max(1, num_inference_steps)

    if not seed or seed.strip() == "":
        seed = int(time.time()) % 4294967295 
    else:
        seed = int(seed)

    width = width - (width % 16)
    height = height - (height % 16)
    init_image = init_image.resize((width, height))

    print(f"\n--- Generating image (Image-to-Image) ---")
    print(f"Model: {model}")
    print(f"Prompt: {prompt}")
    print(f"Init Image Strength: {init_image_strength}")
    print(f"Adjusted Dimensions: {width}x{height}")
    print(f"Desired Steps: {steps}")
    print(f"Adjusted num_inference_steps: {num_inference_steps}")
    print(f"Guidance: {guidance}")
    print(f"LoRA files: {lora_files}")
    print(f"LoRA Scale: {lora_scale}")
    print_memory_usage("Before generation")
    start_time = time.time()

    valid_loras = process_lora_files(lora_files)
    lora_paths = valid_loras if valid_loras else None
    lora_scales = [lora_scale] * len(valid_loras) if valid_loras else None

    flux = get_or_create_flux(
        model,
        None,
        None,
        lora_paths,
        lora_scales
    )
    print_memory_usage("After creating flux")

    timestamp = int(time.time())
    output_filename = f"generated_i2i_{timestamp}.png"
    output_path = os.path.join(OUTPUT_DIR, output_filename)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
        init_image.save(temp.name)
        init_image_path = temp.name

    config = Config(
        num_inference_steps=num_inference_steps,
        guidance=guidance,
        height=height,
        width=width,
        init_image_path=init_image_path,
        init_image_strength=init_image_strength
    )

    image = flux.generate_image(
        seed=seed,
        prompt=prompt,
        config=config
    )

    image.image.save(output_path)
    os.remove(init_image_path)
    duration = time.time() - start_time
    print(f"Image generated in {duration:.2f} seconds. Saved as {output_filename}")
    print_memory_usage("After saving image")

    clear_flux_cache()
    force_mlx_cleanup()

    return image.image, output_filename, prompt

def refresh_lora_choices():
    return gr.update(choices=[name for name, _ in get_available_lora_files()])

demo = None

def refresh_lora_choices():
    return gr.update(choices=[name for name, _ in get_available_lora_files()])

def update_dimensions_on_image_change(image):
    if image is not None:
        width, height = image.size
        return (
            gr.update(value=width),
            gr.update(value=height),
            width,
            height,
            gr.update(value=1.0),
        )
    else:
        return (
            gr.update(value=None),
            gr.update(value=None),
            None,
            None,
            gr.update(value=1.0),
        )

def update_dimensions_on_scale_change(scale_factor, original_width, original_height):
    if original_width is not None and original_height is not None:
        new_width = int(original_width * float(scale_factor))
        new_height = int(original_height * float(scale_factor))
        return gr.update(value=new_width), gr.update(value=new_height)
    else:
        return gr.update(value=None), gr.update(value=None)

def update_height_with_aspect_ratio(width, image):
    if not image or not width:
        return gr.update(value=None)
    original_width, original_height = image.size
    aspect_ratio = original_height / original_width
    new_height = int(float(width) * aspect_ratio)
    return gr.update(value=new_height)

def update_width_with_aspect_ratio(height, image):
    if not image or not height:
        return gr.update(value=None)
    original_width, original_height = image.size
    aspect_ratio = original_width / original_height
    new_width = int(float(height) * aspect_ratio)
    return gr.update(value=new_width)

def scale_dimensions(image, scale_factor):
    if image is not None and scale_factor is not None:
        width, height = image.size
        new_width = int(width * float(scale_factor))
        new_height = int(height * float(scale_factor))
        return new_width, new_height
    else:
        return None, None

def get_random_seed():
    return str(random.randint(0, 2**32 - 1))

def get_random_seed():
    return str(random.randint(0, 2**32 - 1))

def get_system_prompts():
    prompts = {
        "Standard": read_system_prompt(),
        "ControlNet": read_controlnet_prompt()
    }
    return prompts

def create_ui():
    with gr.Blocks(css="""
        .refresh-button {
            background-color: white !important;
            border: 1px solid #ccc !important;
            color: black !important;
            padding: 0px 8px !important;
            height: 38px !important;
            margin-left: -10px !important;
        }
        .refresh-button:hover {
            background-color: #f0f0f0 !important;
        }
    """) as demo:
        with gr.Tabs():
            with gr.TabItem("MFLUX Easy", id=0):
                with gr.Row():
                    with gr.Column():
                        with gr.Group():
                            prompt_simple = gr.Textbox(label="Prompt", lines=2)
                            with gr.Accordion("⚙️ Ollama Settings", open=False) as ollama_section_simple:
                                ollama_components_simple = create_ollama_settings()
                            with gr.Row():
                                enhance_ollama_simple = gr.Button("Enhance prompt with Ollama")
                        
                        ollama_components_simple[2].click(
                            fn=save_settings,
                            inputs=[ollama_components_simple[0], ollama_components_simple[1]],
                            outputs=[ollama_section_simple]
                        )
                        
                        model_simple = gr.Dropdown(
                            choices=get_updated_models(),
                            label="Model",
                            value="schnell-4-bit",
                            allow_custom_value=True
                        )
                        with gr.Row():
                            seed = gr.Textbox(label="Seed (optional)", value="", scale=9)
                            random_seed = gr.Button(
                                "🎲",
                                variant='tool',
                                size='sm',
                                scale=1,
                                min_width=30,
                                elem_classes='refresh-button'
                            )
                        random_seed.click(
                            fn=get_random_seed,
                            outputs=[seed]
                        )
                        image_format = gr.Dropdown(
                            choices=[
                                "Portrait (576x1024)",
                                "Landscape (1024x576)",
                                "Background (1920x1080)",
                                "Square (1024x1024)",
                                "Poster (1080x1920)",
                                "Wide Screen (2560x1440)",
                                "Ultra Wide Screen (3440x1440)",
                                "Banner (728x90)"
                            ],
                            label="Image Format",
                            value="Portrait (576x1024)"
                        )
                        with gr.Row():
                            lora_files_simple = gr.Dropdown(
                                choices=get_lora_choices(),
                                label="Select LoRA Files",
                                multiselect=True,
                                allow_custom_value=True,
                                value=[],
                                interactive=True,
                                scale=9
                            )
                            refresh_lora_simple = gr.Button(
                                "🔄",
                                variant='tool',
                                size='sm',
                                scale=1,
                                min_width=30,
                                elem_classes='refresh-button'
                            )
                        refresh_lora_simple.click(
                            fn=refresh_lora_choices,
                            inputs=[],
                            outputs=[lora_files_simple]
                        )

                        generate_button_simple = gr.Button("Generate Image", variant='primary')
                    
                    with gr.Column():
                        output_image_simple = gr.Image(label="Generated Image")
                        output_filename_simple = gr.Textbox(label="Saved Image Filename")

                enhance_ollama_simple.click(
                    fn=enhance_prompt,
                    inputs=[prompt_simple, ollama_components_simple[0], ollama_components_simple[1]],
                    outputs=prompt_simple
                )
                generate_button_simple.click(
                    fn=simple_generate_image,
                    inputs=[prompt_simple, model_simple, image_format, lora_files_simple, 
                            ollama_components_simple[0], ollama_components_simple[1]],
                    outputs=[output_image_simple, output_filename_simple, prompt_simple]
                )

            with gr.TabItem("Advanced Generate"):
                with gr.Row():
                    with gr.Column():
                        with gr.Group():
                            prompt = gr.Textbox(label="Prompt", lines=2)
                            with gr.Accordion("⚙️ Ollama Settings", open=False) as ollama_section_adv:
                                ollama_components_adv = create_ollama_settings()
                            with gr.Row():
                                enhance_ollama = gr.Button("Enhance prompt with Ollama")
                        
                        ollama_components_adv[2].click(
                            fn=save_settings,
                            inputs=[ollama_components_adv[0], ollama_components_adv[1]],
                            outputs=[ollama_section_adv]
                        )
                        
                        model = gr.Dropdown(
                            choices=get_updated_models(),
                            label="Model",
                            value="schnell-4-bit"
                        )
                        with gr.Row():
                            seed = gr.Textbox(label="Seed (optional)", value="", scale=9)
                            random_seed_adv = gr.Button(
                                "🎲",
                                variant='tool',
                                size='sm',
                                scale=1,
                                min_width=30,
                                elem_classes='refresh-button'
                            )
                        random_seed_adv.click(
                            fn=get_random_seed,
                            outputs=[seed]
                        )
                        with gr.Row():
                            width = gr.Number(label="Width", value=576, precision=0)
                            height = gr.Number(label="Height", value=1024, precision=0)
                        steps = gr.Textbox(label="Inference Steps (optional)", value="")
                        guidance = gr.Number(label="Guidance Scale", value=3.5, visible=False)
                        with gr.Row():
                            lora_files = gr.Dropdown(
                                choices=get_lora_choices(),
                                label="Select LoRA Files",
                                multiselect=True,
                                allow_custom_value=True,
                                value=[],
                                interactive=True,
                                scale=9
                            )
                            refresh_lora_simple = gr.Button(
                                "🔄",
                                variant='tool',
                                size='sm',
                                scale=1,
                                min_width=30,
                                elem_classes='refresh-button'
                            )
                        refresh_lora_simple.click(
                            fn=refresh_lora_choices,
                            inputs=[],
                            outputs=[lora_files]
                        )

                        metadata = gr.Checkbox(label="Export Metadata as JSON", value=False)
                        generate_button = gr.Button("Generate Image", variant='primary')
                    with gr.Column():
                        output_image = gr.Image(label="Generated Image")
                        output_filename = gr.Textbox(label="Saved Image Filename")

                model.change(
                    fn=update_guidance_visibility,
                    inputs=[model],
                    outputs=[guidance]
                )

                enhance_ollama.click(
                    fn=enhance_prompt,
                    inputs=[prompt, ollama_components_adv[0], ollama_components_adv[1]],
                    outputs=prompt
                )
                generate_button.click(
                    fn=generate_image_gradio,
                    inputs=[prompt, model, seed, width, height, steps, guidance, lora_files, metadata,
                            ollama_components_adv[0], ollama_components_adv[1]],
                    outputs=[output_image, output_filename, prompt]
                )

            with gr.TabItem("ControlNet"):
                with gr.Column():
                    prompt_cn = gr.Textbox(label="Prompt", lines=2)
                    control_image = gr.Image(label="Control Image", type='pil')
                    
                    with gr.Accordion("⚙️ Ollama Settings", open=False) as ollama_section_cn:
                        ollama_components_cn = create_ollama_settings(include_prompt_selector=True, prompt_type="controlnet")
                    
                    ollama_model_cn, system_prompt_cn, save_button_cn, prompt_selector_cn = ollama_components_cn

                    gr.Markdown("⚠ Note: If you use the ControlNet Prompt, make sure you are using a vision model.")

                    enhance_ollama_cn = gr.Button("Enhance prompt with Ollama")

                    enhance_ollama_cn.click(
                        fn=enhance_prompt,
                        inputs=[
                            prompt_cn,
                            ollama_model_cn,
                            system_prompt_cn,
                            control_image
                        ],
                        outputs=prompt_cn
                    )

                    save_button_cn.click(
                        fn=save_settings,
                        inputs=[
                            ollama_model_cn, 
                            system_prompt_cn,
                            prompt_selector_cn
                        ],
                        outputs=[ollama_section_cn]
                    )

                with gr.Column():
                    output_image_cn = gr.Image(label="Generated Image")
                    canny_image = gr.Image(label="Canny Image", visible=False)
                    output_message_cn = gr.Textbox(label="Status")

                width_cn = gr.Number(label="Width")
                height_cn = gr.Number(label="Height")
                scale_factor_cn = gr.Slider(
                    minimum=0.0,
                    maximum=2.0,
                    value=1.0,
                    step=0.1,
                    label="Scale Factor (%)"
                )

                original_width_cn = gr.State()
                original_height_cn = gr.State()

                control_image.change(
                    fn=update_dimensions_on_image_change,
                    inputs=[control_image],
                    outputs=[width_cn, height_cn, original_width_cn, original_height_cn, scale_factor_cn]
                )

                scale_factor_cn.change(
                    fn=update_dimensions_on_scale_change,
                    inputs=[scale_factor_cn, original_width_cn, original_height_cn],
                    outputs=[width_cn, height_cn]
                )

                model_cn = gr.Dropdown(
                    choices=get_updated_models(),
                    label="Model",
                    value="schnell-4-bit"
                )
                
                with gr.Row():
                    seed_cn = gr.Textbox(label="Seed (optional)", value="", scale=9)
                    random_seed_cn = gr.Button(
                        "🎲",
                        variant='tool',
                        size='sm',
                        scale=1,
                        min_width=30,
                        elem_classes='refresh-button'
                    )
                random_seed_cn.click(
                    fn=get_random_seed,
                    outputs=[seed_cn]
                )
                steps_cn = gr.Textbox(label="Inference Steps (optional)", value="")
                guidance_cn = gr.Number(label="Guidance Scale", value=3.5, visible=False)
                controlnet_strength = gr.Number(label="ControlNet Strength", value=0.5)

                with gr.Row():
                    lora_files_cn = gr.Dropdown(
                        choices=get_lora_choices(),
                        label="Select LoRA Files",
                        multiselect=True,
                        allow_custom_value=True,
                        value=[],
                        interactive=True,
                        scale=9
                    )
                    refresh_lora_cn = gr.Button(
                        "🔄",
                        variant='tool',
                        size='sm',
                        scale=1,
                        min_width=30,
                        elem_classes='refresh-button'
                    )
                refresh_lora_cn.click(
                    fn=refresh_lora_choices,
                    inputs=[],
                    outputs=[lora_files_cn]
                )

                metadata_cn = gr.Checkbox(label="Export Metadata as JSON", value=False)
                save_canny = gr.Checkbox(label="Save Canny Edge Detection Image", value=False)
                generate_button_cn = gr.Button("Generate Image", variant='primary')

                generate_button_cn.click(
                    fn=generate_image_controlnet_gradio,
                    inputs=[prompt_cn, control_image, model_cn, seed_cn, height_cn, width_cn, steps_cn, 
                            guidance_cn, controlnet_strength, lora_files_cn, metadata_cn, save_canny,
                            ollama_components_cn[0], ollama_components_cn[1]],
                    outputs=[output_image_cn, output_message_cn, prompt_cn, canny_image]
                )

                gr.Markdown("""
                ⚠ Note: Controlnet requires [InstantX/FLUX.1-dev-Controlnet-Canny](https://huggingface.co/InstantX/FLUX.1-dev-Controlnet-Canny), which was trained for the `dev` model. 
                It can work well with `schnell`, but performance is not guaranteed.

                ⚠ Note: The output can be highly sensitive to the controlnet strength and is very much dependent on the reference image. 
                Too high settings will corrupt the image. A recommended starting point is a value like 0.4. Experiment with different strengths to find the best result.
                """)

                save_canny.change(
                    fn=lambda x: gr.update(visible=x),
                    inputs=save_canny,
                    outputs=canny_image
                )

            with gr.TabItem("Image-to-Image", id=3):
                with gr.Row():
                    with gr.Column(scale=1):
                        prompt_i2i = gr.Textbox(label="Prompt", lines=2)
                        with gr.Accordion("⚙️ Ollama Settings", open=False) as ollama_section_i2i:
                            ollama_components_i2i = create_ollama_settings(include_prompt_selector=True, prompt_type="i2i")
                        with gr.Row():
                            enhance_ollama_i2i = gr.Button("Enhance prompt with Ollama")
                        ollama_components_i2i[2].click(
                            fn=save_settings,
                            inputs=[ollama_components_i2i[0], ollama_components_i2i[1]],
                            outputs=[ollama_section_i2i]
                        )

                        init_image = gr.Image(label="Initial Image", type='pil')
                        init_image_strength = gr.Slider(
                            minimum=0.0,
                            maximum=1.0,
                            value=0.3,
                            step=0.01,
                            label="Init Image Strength"
                        )

                        width_i2i = gr.Number(label="Width")
                        height_i2i = gr.Number(label="Height")
                        scale_factor = gr.Slider(
                            minimum=0.0,
                            maximum=2.0,
                            value=1.0,
                            step=0.1,
                            label="Scale Factor (%)"
                        )

                        original_width_i2i = gr.State()
                        original_height_i2i = gr.State()

                        init_image.change(
                            fn=update_dimensions_on_image_change,
                            inputs=[init_image],
                            outputs=[width_i2i, height_i2i, original_width_i2i, original_height_i2i, scale_factor],
                        )

                        scale_factor.change(
                            fn=update_dimensions_on_scale_change,
                            inputs=[scale_factor, original_width_i2i, original_height_i2i],
                            outputs=[width_i2i, height_i2i]
                        )

                        model_i2i = gr.Dropdown(
                            choices=get_updated_models(),
                            label="Model",
                            value="schnell-4-bit"
                        )
                        with gr.Row():
                            seed_i2i = gr.Textbox(label="Seed (optional)", value="", scale=9)
                            random_seed_i2i = gr.Button(
                                "🎲",
                                variant='tool',
                                size='sm',
                                scale=1,
                                min_width=30,
                                elem_classes='refresh-button'
                            )
                        random_seed_i2i.click(
                            fn=get_random_seed,
                            outputs=[seed_i2i]
                        )
                        steps_i2i = gr.Textbox(label="Inference Steps (optional)", value="")
                        guidance_i2i = gr.Number(label="Guidance Scale", value=3.5, visible=False)

                        lora_scale_i2i = gr.Number(label="LoRA Scale", value=1.0)

                        with gr.Row():
                            lora_files_i2i = gr.Dropdown(
                                choices=get_lora_choices(),
                                label="Select LoRA Files",
                                multiselect=True,
                                allow_custom_value=True,
                                value=[],
                                interactive=True,
                                scale=9
                            )
                            refresh_lora_i2i = gr.Button(
                                "🔄",
                                variant='tool',
                                size='sm',
                                scale=1,
                                min_width=30,
                                elem_classes='refresh-button'
                            )
                        refresh_lora_i2i.click(
                            fn=refresh_lora_choices,
                            inputs=[],
                            outputs=[lora_files_i2i]
                        )

                        metadata_i2i = gr.Checkbox(label="Export Metadata as JSON", value=False)
                        generate_button_i2i = gr.Button("Generate Image", variant='primary')

                    with gr.Column(scale=1):
                        output_image_i2i = gr.Image(label="Generated Image")
                        output_filename_i2i = gr.Textbox(label="Saved Image Filename")

                enhance_ollama_i2i.click(
                    fn=enhance_prompt,
                    inputs=[
                        prompt_i2i,
                        ollama_components_i2i[0],
                        ollama_components_i2i[1],
                    ],
                    outputs=prompt_i2i
                )

                generate_button_i2i.click(
                    fn=generate_image_i2i_gradio,
                    inputs=[
                        prompt_i2i,
                        init_image,
                        init_image_strength,
                        model_i2i,
                        seed_i2i,
                        width_i2i,
                        height_i2i,
                        steps_i2i,
                        guidance_i2i,
                        lora_files_i2i,
                        lora_scale_i2i,
                        metadata_i2i,
                        ollama_components_i2i[0],
                        ollama_components_i2i[1],
                    ],
                    outputs=[output_image_i2i, output_filename_i2i, prompt_i2i]
                )

            with gr.TabItem("Model & LoRA Management"):
                gr.Markdown("### Download LoRA")
                lora_source = gr.Radio(
                    choices=["CivitAI", "HuggingFace"],
                    label="LoRA Source",
                    value="CivitAI"
                )
                lora_input = gr.Textbox(label="LoRA Model Page URL (CivitAI) or Model Name (HuggingFace)")

                with gr.Accordion("API Key Settings", open=False):
                    with gr.Row():
                        with gr.Column(scale=3):
                            api_key_input = gr.Textbox(
                                label="CivitAI API Key", 
                                type="password", 
                                value=load_api_key("civitai")
                            )
                            civitai_api_key_status = gr.Markdown(
                                value=f"CivitAI API Key Status: {'Saved' if load_api_key('civitai') else 'Not saved'}"
                            )
                            
                            hf_api_key_input = gr.Textbox(
                                label="HuggingFace API Key", 
                                type="password", 
                                value=load_api_key("huggingface")
                            )
                            hf_api_key_status = gr.Markdown(
                                value=f"HuggingFace API Key Status: {'Saved' if load_api_key('huggingface') else 'Not saved'}"
                            )
                        with gr.Column(scale=1):
                            save_api_keys_button = gr.Button("Save API Keys")
                            clear_api_keys_button = gr.Button("Clear API Keys")
                    gr.Markdown("Don't have an API key? [CivitAI](https://civitai.com/user/account), [HuggingFace](https://huggingface.co/settings/tokens)")

                download_lora_button = gr.Button("Download LoRA", variant='primary')
                lora_download_status = gr.Textbox(label="Download Status", lines=0.5)

                def save_api_keys(api_key_civitai, api_key_hf):
                    save_api_key(api_key_civitai, "civitai")
                    save_api_key(api_key_hf, "huggingface")
                    return (
                        f"CivitAI API Key Status: {'Saved successfully' if api_key_civitai else 'Not saved'}", 
                        f"HuggingFace API Key Status: {'Saved successfully' if api_key_hf else 'Not saved'}"
                    )

                def clear_api_keys():
                    save_api_key("", "civitai")
                    save_api_key("", "huggingface")
                    return "CivitAI API Key Status: Cleared", "HuggingFace API Key Status: Cleared"

                save_api_keys_button.click(
                    fn=save_api_keys,
                    inputs=[api_key_input, hf_api_key_input],
                    outputs=[civitai_api_key_status, hf_api_key_status]
                )

                clear_api_keys_button.click(
                    fn=clear_api_keys,
                    inputs=[],
                    outputs=[civitai_api_key_status, hf_api_key_status]
                )

                def download_lora(model_url_or_name, api_key_civitai, api_key_hf, lora_source):
                    if lora_source == "CivitAI":
                        return download_lora_model(model_url_or_name, api_key_civitai)
                    elif lora_source == "HuggingFace":
                        return download_lora_model_huggingface(model_url_or_name, api_key_hf)
                    else:
                        return gr.update(), gr.update(), gr.update(), "Error: Invalid LoRA source selected"

                download_lora_button.click(
                    fn=download_lora,
                    inputs=[lora_input, api_key_input, hf_api_key_input, lora_source],
                    outputs=[lora_files_simple, lora_files, lora_files_cn, lora_download_status]
                )

                gr.Markdown("## Download and Add Model")
                with gr.Row():
                    with gr.Column(scale=2):
                        hf_model_name = gr.Textbox(label="Hugging Face Model Name")
                        alias = gr.Textbox(label="Model Alias")
                        num_train_steps = gr.Number(label="Number of Training Steps", value=1000)
                        max_sequence_length = gr.Number(label="Max Sequence Length", value=512)
                        
                        with gr.Accordion("API Key Settings", open=False):
                            with gr.Row():
                                with gr.Column(scale=3):
                                    hf_api_key_input = gr.Textbox(
                                        label="HuggingFace API Key", 
                                        type="password", 
                                        value=load_hf_api_key()
                                    )
                                    hf_api_key_status = gr.Markdown(
                                        value=f"HuggingFace API Key Status: {'Saved' if load_hf_api_key() else 'Not saved'}"
                                    )
                                with gr.Column(scale=1):
                                    save_hf_api_key_button = gr.Button("Save API Key")
                                    clear_hf_api_key_button = gr.Button("Clear API Key")
                            gr.Markdown("Don't have an API key? [Create one here](https://huggingface.co/settings/tokens)")
                        
                    with gr.Column(scale=1):
                        download_button = gr.Button("Download and Add Model", variant='primary')
                        download_output = gr.Textbox(label="Download Status", lines=3)

                def save_hf_api_key_handler(key):
                    save_hf_api_key(key)
                    return f"HuggingFace API Key Status: {'Saved successfully' if key else 'Not saved'}"

                def clear_hf_api_key_handler():
                    save_hf_api_key("")
                    return "HuggingFace API Key Status: Cleared"

                save_hf_api_key_button.click(
                    fn=save_hf_api_key_handler,
                    inputs=[hf_api_key_input],
                    outputs=[hf_api_key_status]
                )

                clear_hf_api_key_button.click(
                    fn=clear_hf_api_key_handler,
                    inputs=[],
                    outputs=[hf_api_key_status]
                )

                download_button.click(
                    fn=download_and_save_model,
                    inputs=[hf_model_name, alias, num_train_steps, max_sequence_length, hf_api_key_input],
                    outputs=[download_output]
                )

                gr.Markdown("## Quantize Model")
                with gr.Row():
                    with gr.Column(scale=2):
                        model_quant = gr.Dropdown(
                            choices=[m for m in get_updated_models() if not m.endswith("-4-bit") and not m.endswith("-8-bit")],
                            label="Model to Quantize",
                            value="dev"
                        )
                        quantize_level = gr.Radio(choices=["4", "8"], label="Quantize Level", value="8")
                    with gr.Column(scale=1):
                        save_button = gr.Button("Save Quantized Model", variant='primary')
                        save_output = gr.Textbox(label="Quantization Output", lines=3)

                save_button.click(
                    fn=save_quantized_model_gradio,
                    inputs=[model_quant, quantize_level],
                    outputs=[model_simple, model, model_cn, model_quant, save_output]
                )

    return demo

demo = create_ui()

if __name__ == "__main__":
    demo.queue().launch(show_error=True)