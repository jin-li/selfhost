"""Checked integration for pinned Chatterbox-TTS-Server 915ae289."""
import ast
from pathlib import Path
import py_compile

STARTUP = '''        if not engine.load_model():
            logger.critical(
                "CRITICAL: TTS Model failed to load on startup. Server might not function correctly."
            )
        else:
            logger.info("TTS Model loaded successfully via engine.")
            host_address = get_host()
            server_port = get_port()
            browser_thread = threading.Thread(
                target=lambda: _delayed_browser_open(host_address, server_port),
                daemon=True,
            )
            browser_thread.start()
'''
MAIN = '# --- Main Execution ---\n'
STREAM_EXECUTOR = """                audio_tensor, chunk_sr = await loop.run_in_executor(
                    None,
                    lambda c=chunk_text: engine.synthesize("""
STREAM_COMPLETE = """                audio_tensor, chunk_sr = await _complete_sync(
                    lambda c=chunk_text: engine.synthesize("""


MODEL_INFO_OLD = '''def get_model_info() -> dict:
    """
    Returns information about the currently loaded model.
    Used by the API to expose model details to the UI.

    Returns:
        Dictionary containing model information
    """
    return {
        "loaded": MODEL_LOADED,
        "type": loaded_model_type,  # "original", "turbo", or "multilingual"
        "class_name": loaded_model_class_name,
        "device": model_device,
        "sample_rate": chatterbox_model.sr if chatterbox_model else None,
        "supports_paralinguistic_tags": loaded_model_type == "turbo",
        "available_paralinguistic_tags": (
            TURBO_PARALINGUISTIC_TAGS if loaded_model_type == "turbo" else []
        ),
        "turbo_available_in_package": TURBO_AVAILABLE,
        "multilingual_available_in_package": MULTILINGUAL_AVAILABLE,
        "supports_multilingual": loaded_model_type == "multilingual",
        "supported_languages": (
            SUPPORTED_LANGUAGES if loaded_model_type == "multilingual" else {"en": "English"}
        ),
    }
'''
MODEL_INFO_NEW = '''def get_model_info() -> dict:
    """
    Returns information about the currently loaded model.
    Used by the API to expose model details to the UI.

    Returns:
        Dictionary containing model information
    """
    # Idle discovery describes configured capabilities without allocating weights.
    configured_type = MODEL_SELECTOR_MAP.get(
        config_manager.get_string("model.repo_id", "chatterbox-turbo").lower().strip(),
        "original",
    )
    # Snapshot references once. Unload may release the GIL in tensor destructors;
    # an incomplete transition is idle, never a partially loaded health record.
    loaded, model, model_type, class_name, device = (
        MODEL_LOADED, chatterbox_model, loaded_model_type,
        loaded_model_class_name, model_device,
    )
    loaded = bool(loaded and model is not None and model_type and class_name and device)
    info_type = model_type if loaded else configured_type
    sample_rate = model.sr if loaded else None
    return {
        "loaded": loaded,
        "type": info_type,
        "metadata_source": "loaded" if loaded else "configured",
        "class_name": class_name if loaded else None,
        "device": device if loaded else None,
        "sample_rate": sample_rate,
        "supports_paralinguistic_tags": info_type == "turbo",
        "available_paralinguistic_tags": (
            TURBO_PARALINGUISTIC_TAGS if info_type == "turbo" else []
        ),
        "turbo_available_in_package": TURBO_AVAILABLE,
        "multilingual_available_in_package": MULTILINGUAL_AVAILABLE,
        "supports_multilingual": info_type == "multilingual",
        "supported_languages": (
            SUPPORTED_LANGUAGES if info_type == "multilingual" else {"en": "English"}
        ),
    }
'''


def patch(root):
    server = root / 'server.py'
    source = server.read_text()
    engine = root / 'engine.py'
    engine_source = engine.read_text()
    if engine_source.count(MODEL_INFO_OLD) != 1:
        raise RuntimeError('Pinned engine model-info function no longer matches')
    engine_source = engine_source.replace(MODEL_INFO_OLD, MODEL_INFO_NEW, 1)
    release = '        del chatterbox_model\n        chatterbox_model = None'
    if engine_source.count(release) != 2:
        raise RuntimeError('Pinned unload/reload model release no longer matches')
    # Direct assignment updates the global before the old model destructor runs.
    # del temporarily removes the global while GPU destructors may release GIL.
    engine_source = engine_source.replace(release, '        chatterbox_model = None')
    if any(isinstance(node, ast.Delete)
           and any(isinstance(target, ast.Name) and target.id == 'chatterbox_model'
                   for target in node.targets)
           for node in ast.walk(ast.parse(engine_source))):
        raise RuntimeError('Unsafe model global deletion remains')
    compile(engine_source, str(engine), 'exec')
    tree = ast.parse(source)
    routes = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id == 'app'
                        and decorator.func.attr == 'post' and decorator.args
                        and isinstance(decorator.args[0], ast.Constant)):
                    routes[decorator.args[0].value] = node.name
    expected = {'/tts': 'custom_tts_endpoint', '/v1/audio/speech': 'openai_speech_endpoint',
                '/save_settings': 'save_settings_endpoint', '/reset_settings': 'reset_settings_endpoint',
                '/restart_server': 'restart_server_endpoint', '/api/unload': 'unload_model_endpoint'}
    if any(routes.get(path) != name for path, name in expected.items()):
        raise RuntimeError('Pinned model lifecycle routes no longer match')
    if source.count(STARTUP) != 1 or source.count(MAIN) != 1:
        raise RuntimeError('Pinned model lifecycle integration no longer matches')
    if source.count(STREAM_EXECUTOR) != 1:
        raise RuntimeError('Pinned streaming synthesis executor no longer matches')
    source = source.replace(STREAM_EXECUTOR, STREAM_COMPLETE, 1)
    source = source.replace(STARTUP, '        logger.info("Model loading deferred until a speech request.")\n', 1)
    source = source.replace(MAIN, 'from lifecycle import ModelLifecycle, _complete_sync\n'
                            'app = ModelLifecycle(app, engine)\n'
                            'from tts_runtime import RecycleAfterRequest\n'
                            'app = RecycleAfterRequest(app)\n\n\n' + MAIN, 1)
    # No startup load should remain outside request handlers.
    lifespan = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'lifespan')
    if any(isinstance(n, ast.Attribute) and n.attr == 'load_model' for n in ast.walk(lifespan)):
        raise RuntimeError('Startup still loads a model')
    compile(source, str(server), 'exec')
    engine.write_text(engine_source)
    py_compile.compile(str(engine), doraise=True)
    server.write_text(source)
    py_compile.compile(str(server), doraise=True)


if __name__ == '__main__':
    patch(Path('/app'))
