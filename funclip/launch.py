#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
# Copyright FunASR (https://github.com/alibaba-damo-academy/FunClip). All Rights Reserved.
#  MIT License  (https://opensource.org/licenses/MIT)

from http import server
import os
import logging
import argparse
import tempfile
from datetime import datetime
import gradio as gr
# REQ-20260926-NNN：funasr / videoclipper 的 import 必须延后到原始 FunClip 分支。
# 否则即使走 slirn 分支，模块级导入也会触发 torch.multiprocessing.spawn 子进程
# → 启动 pyenv pythonw 副本争 7861，导致服务卡死。
from llm.openai_api import openai_call
from llm.qwen_api import call_qwen_model
from utils.trans_utils import extract_timestamps
from introduction import top_md_1, top_md_3, top_md_4


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='argparse testing')
    parser.add_argument('--lang', '-l', type=str, default = "zh", help="language")
    parser.add_argument('--model', '-m', type=str, default="paraformer", choices=["paraformer", "fun-asr-nano", "sensevoice"], help="ASR model: paraformer, fun-asr-nano, or sensevoice")
    parser.add_argument('--share', '-s', action='store_true', help="if to establish gradio share link")
    parser.add_argument('--port', '-p', type=int, default=7860, help='port number')
    parser.add_argument('--listen', action='store_true', help="if to listen to all hosts")
    parser.add_argument('--home', type=str, default="original", choices=["original", "slirn"], help="which home page to launch: original (upstream FunClip) or slirn (custom home)")
    args = parser.parse_args()

    # 公开版：字体不入库，启动时尽力获取（约 54MB，仅首次）。
    # 失败不阻断启动——渲染字幕时会惰性重试，彻底失败则走系统字体回退。
    import sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parent.parent
    if str(_root) not in _sys.path:
        _sys.path.insert(0, str(_root))
    try:
        from slirn_home.font_provider import ensure_font
        ensure_font()
    except Exception as _e:  # noqa: BLE001
        print(f"[font] 字体获取失败（不影响启动）: {_e}", flush=True)

    # ---- Slirn 自定义首页分支（REQ-20260914-001-B）----
    if args.home == "slirn":
        print("[launch.py] entering slirn branch", flush=True)
        import sys
        from pathlib import Path
        # 把 funclip-main 仓库根加到 sys.path（slirn_home 在那里）
        funclip_main_root = Path(__file__).resolve().parent.parent
        if str(funclip_main_root) not in sys.path:
            sys.path.insert(0, str(funclip_main_root))
        from slirn_home import build_app, slirn_home_static_css, _register_slirn_api
        # Monkey-patch httpx.get — Gradio 6 在 launch() 末尾做 startup-events 自检，
        # Windows 上 httpx 连自己 (127.0.0.1) 会偶发失败并 timeout=None 永久阻塞。
        # 用假响应绕过这个自检，让 launch(prevent_thread_lock=True) 能正常返回。
        import httpx as _httpx
        _orig_get = _httpx.get
        class _FakeResp:
            is_success = True
            status_code = 200
            url = "http://127.0.0.1/startup-events"
        def _patched_get(url, *a, **kw):
            if "startup-events" in str(url):
                return _FakeResp()
            return _orig_get(url, *a, **kw)
        _httpx.get = _patched_get

        slirn_app = build_app()
        # slirn 首页默认端口 7861（D6：与上游首页端口分离）
        slirn_port = args.port if args.port != 7860 else 7861
        server_name = '0.0.0.0' if args.listen else '127.0.0.1'
        # Gradio 6 在 launch() 内部会用 App.create_app(...) 重建 self.app，
        # build_app() 里注册的 /slirn/api/* 路由会被丢弃。
        # 用 prevent_thread_lock=True 让 launch() 立刻返回，然后我们在新的 self.app 上重新注册路由。
        try:
            slirn_app.launch(
                share=args.share,
                server_port=slirn_port,
                server_name=server_name,
                inbrowser=False if args.listen else True,
                theme=gr.themes.Soft(),
                css=slirn_home_static_css(),
                # 不再传 head= — Gradio 6.17.3 的 launch(head=...) 不会渲染到页面 <head>。
                # JS 注入改在 gr.HTML 组件的 head= 参数上（见 slirn_home/app.py build_app）。
                prevent_thread_lock=True,
            )
            # launch() 已重新创建 app — 在新 app 上重新注册 /slirn/api/* 路由
            from slirn_home.paths import find_slirn_standalone_root as _find_root
            from tasklib import TaskManager as _TM
            from slirn_home.auth import AuthStore as _AuthStore, install_auth_middleware as _install_auth
            from slirn_home.app import slirn_task_access as _slirn_task_access
            _mgr = _TM(_find_root())
            _auth = _AuthStore(_find_root())
            # REQ-20261021-141：默认 admin（admin / admin123!@#，不存在时创建）
            if _auth.ensure_default_admin():
                print("[slirn] 已创建默认管理员 admin（密码 admin123!@#），"
                      "建议登录后尽快修改密码", flush=True)
            _register_slirn_api(slirn_app, _mgr, _find_root(), _auth)
            # REQ-20260926-NNN：Gradio 6 launch() 重建 self.app 会丢 middleware，必须重挂
            _install_auth(slirn_app.app, _auth,
                          task_access=_slirn_task_access(_mgr, _auth))
            print(f"[slirn] custom /slirn/api/* routes re-registered on live app", flush=True)
            # 2026-10-28：清理「孤儿 running」执行记录 —— 服务重启时正在跑的
            # job 被杀，其 record_start 已落盘但 record_finish 永远不会来，
            # 执行日志里那条会永远显示「执行中」（用户以为还在跑）。
            try:
                from slirn_home import execution_history as _exh
                _n = 0
                for _t in _mgr.list():
                    try:
                        _n += _exh.mark_orphans_failed(
                            Path(_mgr.tasks_dir) / _t.task_id / "outputs")
                    except Exception:  # noqa: BLE001 — 单任务清理失败不影响启动
                        continue
                if _n:
                    print(f"[slirn] 已标记 {_n} 条中断的执行记录（服务重启）", flush=True)
            except Exception as _e:  # noqa: BLE001 — 清理失败不影响启动
                print(f"[slirn] 执行记录清理跳过: {_e}", flush=True)
            # REQ-20260922-NNN-b：把 launch() 实际解析出的端口回写给 pipeline 调度器。
            # Gradio 在 server_port 被占用时会自动 +1（7861 被旧实例占用 → 新实例
            # 落 7862）；不回写的话调度器仍按 7861 自调 → WinError 10061 连接拒绝。
            # setdefault：用户显式设置的 SLIRN_API_BASE 优先。
            _live_port = getattr(slirn_app, "server_port", None)
            if _live_port:
                import os as _os
                _os.environ.setdefault(
                    "SLIRN_API_BASE", f"http://127.0.0.1:{_live_port}/slirn/api")
                print(f"[slirn] pipeline scheduler → 127.0.0.1:{_live_port}/slirn/api", flush=True)
            import time
            while True:
                time.sleep(3600)
        except Exception as e:
            print(f"[slirn] launch 自检失败但服务可能仍在运行: {e}", flush=True)
            # 即使 launch() 抛错，uvicorn 已经在后台跑 — 重新注册路由
            try:
                from slirn_home.paths import find_slirn_standalone_root as _find_root
                from tasklib import TaskManager as _TM
                from slirn_home.auth import AuthStore as _AuthStore, install_auth_middleware as _install_auth
                from slirn_home.app import slirn_task_access as _slirn_task_access
                _mgr = _TM(_find_root())
                _auth = _AuthStore(_find_root())
                _auth.ensure_default_admin()   # REQ-20261021-141：与正常路径一致
                _register_slirn_api(slirn_app, _mgr, _find_root(), _auth)
                _install_auth(slirn_app.app, _auth,
                              task_access=_slirn_task_access(_mgr, _auth))
                print(f"[slirn] custom /slirn/api/* routes re-registered (recovery path)", flush=True)
                # REQ-20260922-NNN-b：恢复路径同样回写实际端口（见上方成功路径注释）
                _live_port = getattr(slirn_app, "server_port", None)
                if _live_port:
                    import os as _os
                    _os.environ.setdefault(
                        "SLIRN_API_BASE", f"http://127.0.0.1:{_live_port}/slirn/api")
                    print(f"[slirn] pipeline scheduler → 127.0.0.1:{_live_port}/slirn/api", flush=True)
            except Exception as e2:
                print(f"[slirn] re-register failed: {e2}", flush=True)
            import time
            while True:
                time.sleep(3600)
        sys.exit(0)
    
    # REQ-20260926-NNN：原始 FunClip 分支才需要 funasr/videoclipper（slirn 分支不需要）。
    # 必须延后到这里，否则 torch.multiprocessing.spawn 会立刻启 pyenv pythonw 副本争 7861。
    from funasr import AutoModel
    from videoclipper import VideoClipper

    if args.lang == 'zh':
        if hasattr(args, 'model') and args.model == 'fun-asr-nano':
            funasr_model = AutoModel(model="FunAudioLLM/Fun-ASR-Nano-2512",
                                    trust_remote_code=True,
                                    remote_code="./model.py",
                                    vad_model="fsmn-vad",
                                    vad_kwargs={"max_single_segment_time": 30000},
                                    spk_model="cam++",
                                    hub="hf",
                                    )
        elif hasattr(args, 'model') and args.model == 'sensevoice':
            funasr_model = AutoModel(model="iic/SenseVoiceSmall",
                                    vad_model="fsmn-vad",
                                    vad_kwargs={"max_single_segment_time": 30000},
                                    spk_model="cam++",
                                    )
        else:
            funasr_model = AutoModel(model="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                                    vad_model="damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                                    punc_model="damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
                                    spk_model="damo/speech_campplus_sv_zh-cn_16k-common",
                                    )
    else:
        funasr_model = AutoModel(model="iic/speech_paraformer_asr-en-16k-vocab4199-pytorch",
                                vad_model="damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                                punc_model="damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
                                spk_model="damo/speech_campplus_sv_zh-cn_16k-common",
                                )
    audio_clipper = VideoClipper(funasr_model)
    audio_clipper.lang = args.lang
    
    server_name='127.0.0.1'
    if args.listen:
        server_name = '0.0.0.0'
        
    def save_text_to_file(content, extension, output_dir=None):
        if not content:
            return None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"result_{timestamp}.{extension}"
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            file_path = os.path.join(output_dir, filename)
        else:
            # Create a temporary file
            temp_dir = tempfile.gettempdir()
            file_path = os.path.join(temp_dir, filename)
            
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return file_path

    def audio_recog(audio_input, sd_switch, hotwords, output_dir):
        return audio_clipper.recog(audio_input, sd_switch, None, hotwords, output_dir=output_dir)

    def video_recog(video_input, sd_switch, hotwords, output_dir):
        return audio_clipper.video_recog(video_input, sd_switch, hotwords, output_dir=output_dir)

    def video_clip(dest_text, video_spk_input, start_ost, end_ost, state, output_dir):
        return audio_clipper.video_clip(
            dest_text, start_ost, end_ost, state, dest_spk=video_spk_input, output_dir=output_dir
            )

    def mix_recog(video_input, audio_input, hotwords, output_dir):
        output_dir = output_dir.strip()
        if not len(output_dir):
            output_dir = None
        else:
            output_dir = os.path.abspath(output_dir)
        audio_state, video_state = None, None
        if video_input is not None:
            res_text, res_srt, video_state = video_recog(
                video_input, 'No', hotwords, output_dir=output_dir)
            text_file = save_text_to_file(res_text, 'txt', output_dir)
            srt_file = save_text_to_file(res_srt, 'srt', output_dir)
            return res_text, res_srt, video_state, None, text_file, srt_file
        if audio_input is not None:
            res_text, res_srt, audio_state = audio_recog(
                audio_input, 'No', hotwords, output_dir=output_dir)
            text_file = save_text_to_file(res_text, 'txt', output_dir)
            srt_file = save_text_to_file(res_srt, 'srt', output_dir)
            return res_text, res_srt, None, audio_state, text_file, srt_file
    
    def mix_recog_speaker(video_input, audio_input, hotwords, output_dir):
        output_dir = output_dir.strip()
        if not len(output_dir):
            output_dir = None
        else:
            output_dir = os.path.abspath(output_dir)
        audio_state, video_state = None, None
        if video_input is not None:
            res_text, res_srt, video_state = video_recog(
                video_input, 'Yes', hotwords, output_dir=output_dir)
            text_file = save_text_to_file(res_text, 'txt', output_dir)
            srt_file = save_text_to_file(res_srt, 'srt', output_dir)
            return res_text, res_srt, video_state, None, text_file, srt_file
        if audio_input is not None:
            res_text, res_srt, audio_state = audio_recog(
                audio_input, 'Yes', hotwords, output_dir=output_dir)
            text_file = save_text_to_file(res_text, 'txt', output_dir)
            srt_file = save_text_to_file(res_srt, 'srt', output_dir)
            return res_text, res_srt, None, audio_state, text_file, srt_file
    
    def mix_clip(dest_text, video_spk_input, start_ost, end_ost, video_state, audio_state, output_dir):
        output_dir = output_dir.strip()
        if not len(output_dir):
            output_dir = None
        else:
            output_dir = os.path.abspath(output_dir)
        if video_state is not None:
            clip_video_file, message, clip_srt = audio_clipper.video_clip(
                dest_text, start_ost, end_ost, video_state, dest_spk=video_spk_input, output_dir=output_dir)
            return clip_video_file, None, message, clip_srt
        if audio_state is not None:
            (sr, res_audio), message, clip_srt = audio_clipper.clip(
                dest_text, start_ost, end_ost, audio_state, dest_spk=video_spk_input, output_dir=output_dir)
            return None, (sr, res_audio), message, clip_srt
    
    def video_clip_addsub(dest_text, video_spk_input, start_ost, end_ost, state, output_dir, font_size, font_color):
        output_dir = output_dir.strip()
        if not len(output_dir):
            output_dir = None
        else:
            output_dir = os.path.abspath(output_dir)
        return audio_clipper.video_clip(
            dest_text, start_ost, end_ost, state, 
            font_size=font_size, font_color=font_color, 
            add_sub=True, dest_spk=video_spk_input, output_dir=output_dir
            )
        
    def llm_inference(system_content, user_content, srt_text, model, apikey, video_state=None, audio_state=None):
        SUPPORT_LLM_PREFIX = ['qwen', 'gpt', 'g4f', 'moonshot', 'deepseek']
        # Dynamically calculate video/audio duration from state
        duration_minutes = 0
        if video_state is not None and 'video' in video_state:
            duration_minutes = round(video_state['video'].duration / 60, 1)
        elif audio_state is not None and 'audio_input' in audio_state:
            sr, data = audio_state['audio_input']
            duration_minutes = round(len(data) / sr / 60, 1)

        # Append actual video duration to prompt
        if duration_minutes > 0:
            duration_note = f"\n\n【重要】当前视频的实际时长为 {duration_minutes} 分钟，请根据此实际时长生成符合要求的输出（要求不低于 {int(duration_minutes * 0.5)} 分钟）。"
            system_content = system_content + duration_note

        if model.startswith('qwen'):
            return call_qwen_model(apikey, model, user_content+'\n'+srt_text, system_content)
        if model.startswith('gpt') or model.startswith('moonshot') or model.startswith('deepseek'):
            return openai_call(apikey, model, system_content, user_content+'\n'+srt_text)
        elif model.startswith('g4f'):
            # g4f 已降为可选依赖（requirements.txt 注释说明）— 仅 original 模式
            # 选 g4f 前缀模型时才需要，延迟导入避免没装时整个服务起不来。
            from llm.g4f_openai_api import g4f_openai_call
            model = "-".join(model.split('-')[1:])
            return g4f_openai_call(model, system_content, user_content+'\n'+srt_text)
        else:
            logging.error("LLM name error, only {} are supported as LLM name prefix."
                          .format(SUPPORT_LLM_PREFIX))
    
    def AI_clip(LLM_res, dest_text, video_spk_input, start_ost, end_ost, video_state, audio_state, output_dir):
        timestamp_list = extract_timestamps(LLM_res)
        output_dir = output_dir.strip()
        if not len(output_dir):
            output_dir = None
        else:
            output_dir = os.path.abspath(output_dir)
        if video_state is not None:
            clip_video_file, message, clip_srt = audio_clipper.video_clip(
                dest_text, start_ost, end_ost, video_state, 
                dest_spk=video_spk_input, output_dir=output_dir, timestamp_list=timestamp_list, add_sub=False)
            return clip_video_file, None, message, clip_srt
        if audio_state is not None:
            (sr, res_audio), message, clip_srt = audio_clipper.clip(
                dest_text, start_ost, end_ost, audio_state, 
                dest_spk=video_spk_input, output_dir=output_dir, timestamp_list=timestamp_list, add_sub=False)
            return None, (sr, res_audio), message, clip_srt
    
    def AI_clip_subti(LLM_res, dest_text, video_spk_input, start_ost, end_ost, video_state, audio_state, output_dir):
        timestamp_list = extract_timestamps(LLM_res)
        output_dir = output_dir.strip()
        if not len(output_dir):
            output_dir = None
        else:
            output_dir = os.path.abspath(output_dir)
        if video_state is not None:
            clip_video_file, message, clip_srt = audio_clipper.video_clip(
                dest_text, start_ost, end_ost, video_state, 
                dest_spk=video_spk_input, output_dir=output_dir, timestamp_list=timestamp_list, add_sub=True)
            return clip_video_file, None, message, clip_srt
        if audio_state is not None:
            (sr, res_audio), message, clip_srt = audio_clipper.clip(
                dest_text, start_ost, end_ost, audio_state, 
                dest_spk=video_spk_input, output_dir=output_dir, timestamp_list=timestamp_list, add_sub=True)
            return None, (sr, res_audio), message, clip_srt
    
    # gradio interface
    theme = gr.Theme.load("funclip/utils/theme.json")
    with gr.Blocks(theme=theme) as funclip_service:
        gr.Markdown(top_md_1)
        # gr.Markdown(top_md_2)
        gr.Markdown(top_md_3)
        gr.Markdown(top_md_4)
        video_state, audio_state = gr.State(), gr.State()
        with gr.Row():
            with gr.Column():
                with gr.Row():
                    video_input = gr.Video(label="视频输入 | Video Input")
                    audio_input = gr.Audio(label="音频输入 | Audio Input")
                with gr.Column():
                    gr.Examples(['https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ClipVideo/%E4%B8%BA%E4%BB%80%E4%B9%88%E8%A6%81%E5%A4%9A%E8%AF%BB%E4%B9%A6%EF%BC%9F%E8%BF%99%E6%98%AF%E6%88%91%E5%90%AC%E8%BF%87%E6%9C%80%E5%A5%BD%E7%9A%84%E7%AD%94%E6%A1%88-%E7%89%87%E6%AE%B5.mp4', 
                                 'https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ClipVideo/2022%E4%BA%91%E6%A0%96%E5%A4%A7%E4%BC%9A_%E7%89%87%E6%AE%B52.mp4', 
                                 'https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ClipVideo/%E4%BD%BF%E7%94%A8chatgpt_%E7%89%87%E6%AE%B5.mp4'],
                                [video_input],
                                label='示例视频 | Demo Video')
                    gr.Examples(['https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ClipVideo/%E8%AE%BF%E8%B0%88.mp4'],
                                [video_input],
                                label='多说话人示例视频 | Multi-speaker Demo Video')
                    gr.Examples(['https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ClipVideo/%E9%B2%81%E8%82%83%E9%87%87%E8%AE%BF%E7%89%87%E6%AE%B51.wav'],
                                [audio_input],
                                label="示例音频 | Demo Audio")
                    with gr.Column():
                        # with gr.Row():
                            # video_sd_switch = gr.Radio(["No", "Yes"], label="👥区分说话人 Get Speakers", value='No')
                        hotwords_input = gr.Textbox(label="🚒 热词 | Hotwords(可以为空，多个热词使用空格分隔，仅支持中文热词)")
                        output_dir = gr.Textbox(label="📁 文件输出路径 | File Output Dir (可以为空，Linux, mac系统可以稳定使用)", value=" ")
                        with gr.Row():
                            recog_button = gr.Button("👂 识别 | ASR", variant="primary")
                            recog_button2 = gr.Button("👂👫 识别+区分说话人 | ASR+SD")
                video_text_output = gr.Textbox(label="✏️ 识别结果 | Recognition Result")
                video_srt_output = gr.Textbox(label="📖 SRT字幕内容 | RST Subtitles")
                with gr.Row():
                    video_text_file = gr.File(label="⬇️ 下载识别结果 | Download Recognition Result", interactive=False)
                    video_srt_file = gr.File(label="⬇️ 下载SRT字幕 | Download SRT Subtitles", interactive=False)
            with gr.Column():
                with gr.Tab("🧠 LLM智能裁剪 | LLM Clipping"):
                    with gr.Column():
                        prompt_head = gr.Textbox(label="Prompt System (按需更改，最好不要变动主体和要求)", value=("你是一个视频srt字幕分析剪辑器，输入视频的srt字幕，"
                                "分析其中的精彩且尽可能连续的片段并裁剪出来，输出四条以内的片段，将片段中在时间上连续的多个句子及它们的时间戳合并为一条，"
                                "注意确保文字与时间戳的正确匹配。输出需严格按照如下格式：1. [开始时间-结束时间] 文本，注意其中的连接符是“-”"))
                        prompt_head2 = gr.Textbox(label="Prompt User（不需要修改，会自动拼接左下角的srt字幕）", value=("这是待裁剪的视频srt字幕："))
                        with gr.Column():
                            with gr.Row():
                                llm_model = gr.Dropdown(
                                    choices=[
                                        "deepseek-chat",
                                        "qwen-plus",
                                             "gpt-3.5-turbo", 
                                             "gpt-3.5-turbo-0125", 
                                             "gpt-4-turbo",
                                             "g4f-gpt-3.5-turbo"], 
                                    value="deepseek-chat",
                                    label="LLM Model Name",
                                    allow_custom_value=True)
                                apikey_input = gr.Textbox(label="APIKEY")
                            llm_button =  gr.Button("LLM推理 | LLM Inference（首先进行识别，非g4f需配置对应apikey）", variant="primary")
                        llm_result = gr.Textbox(label="LLM Clipper Result")
                        with gr.Row():
                            llm_clip_button = gr.Button("🧠 LLM智能裁剪 | AI Clip", variant="primary")
                            llm_clip_subti_button = gr.Button("🧠 LLM智能裁剪+字幕 | AI Clip+Subtitles")
                with gr.Tab("✂️ 根据文本/说话人裁剪 | Text/Speaker Clipping"):
                    video_text_input = gr.Textbox(label="✏️ 待裁剪文本 | Text to Clip (多段文本使用'#'连接)")
                    video_spk_input = gr.Textbox(label="✏️ 待裁剪说话人 | Speaker to Clip (多个说话人使用'#'连接)")
                    with gr.Row():
                        clip_button = gr.Button("✂️ 裁剪 | Clip", variant="primary")
                        clip_subti_button = gr.Button("✂️ 裁剪+字幕 | Clip+Subtitles")
                    with gr.Row():
                        video_start_ost = gr.Slider(minimum=-500, maximum=1000, value=0, step=50, label="⏪ 开始位置偏移 | Start Offset (ms)")
                        video_end_ost = gr.Slider(minimum=-500, maximum=1000, value=100, step=50, label="⏩ 结束位置偏移 | End Offset (ms)")
                with gr.Row():
                    font_size = gr.Slider(minimum=10, maximum=100, value=32, step=2, label="🔠 字幕字体大小 | Subtitle Font Size")
                    font_color = gr.Radio(["black", "white", "green", "red"], label="🌈 字幕颜色 | Subtitle Color", value='white')
                    # font = gr.Radio(["黑体", "Alibaba Sans"], label="字体 Font")
                video_output = gr.Video(label="裁剪结果 | Video Clipped")
                audio_output = gr.Audio(label="裁剪结果 | Audio Clipped")
                clip_message = gr.Textbox(label="⚠️ 裁剪信息 | Clipping Log")
                srt_clipped = gr.Textbox(label="📖 裁剪部分SRT字幕内容 | Clipped RST Subtitles")            
                
        recog_button.click(mix_recog, 
                            inputs=[video_input, 
                                    audio_input, 
                                    hotwords_input, 
                                    output_dir,
                                    ], 
                            outputs=[video_text_output, video_srt_output, video_state, audio_state, video_text_file, video_srt_file])
        recog_button2.click(mix_recog_speaker, 
                            inputs=[video_input, 
                                    audio_input, 
                                    hotwords_input, 
                                    output_dir,
                                    ], 
                            outputs=[video_text_output, video_srt_output, video_state, audio_state, video_text_file, video_srt_file])
        clip_button.click(mix_clip, 
                           inputs=[video_text_input, 
                                   video_spk_input, 
                                   video_start_ost, 
                                   video_end_ost, 
                                   video_state, 
                                   audio_state, 
                                   output_dir
                                   ],
                           outputs=[video_output, audio_output, clip_message, srt_clipped])
        clip_subti_button.click(video_clip_addsub, 
                           inputs=[video_text_input, 
                                   video_spk_input, 
                                   video_start_ost, 
                                   video_end_ost, 
                                   video_state, 
                                   output_dir, 
                                   font_size, 
                                   font_color,
                                   ], 
                           outputs=[video_output, clip_message, srt_clipped])
        llm_button.click(llm_inference,
                         inputs=[prompt_head, prompt_head2, video_srt_output, llm_model, apikey_input, video_state, audio_state],
                         outputs=[llm_result])
        llm_clip_button.click(AI_clip, 
                           inputs=[llm_result,
                                   video_text_input, 
                                   video_spk_input, 
                                   video_start_ost, 
                                   video_end_ost, 
                                   video_state, 
                                   audio_state, 
                                   output_dir,
                                   ],
                           outputs=[video_output, audio_output, clip_message, srt_clipped])
        llm_clip_subti_button.click(AI_clip_subti, 
                           inputs=[llm_result,
                                   video_text_input, 
                                   video_spk_input, 
                                   video_start_ost, 
                                   video_end_ost, 
                                   video_state, 
                                   audio_state, 
                                   output_dir,
                                   ],
                           outputs=[video_output, audio_output, clip_message, srt_clipped])
    
    # start gradio service in local or share
    if args.listen:
        funclip_service.launch(share=args.share, server_port=args.port, server_name=server_name, inbrowser=False)
    else:
        funclip_service.launch(share=args.share, server_port=args.port, server_name=server_name)
