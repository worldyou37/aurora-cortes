#!/usr/bin/env python3
"""Local browser UI for the authorized video clipping pipeline."""
from __future__ import annotations
import json, logging, os, re, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs
import socket, sys, urllib.request, webbrowser, errno
from datetime import datetime, timezone
import importlib.util, importlib.metadata, shutil, subprocess
from logging.handlers import RotatingFileHandler
import main as pipeline

ROOT=Path(__file__).resolve().parent
WEB=ROOT/"web"
PORT=int(os.getenv("CLIPPER_PORT","8774"))
HOST=os.getenv("CLIPPER_HOST","0.0.0.0")
MAX_UPLOAD_BYTES=8*1024*1024*1024
APP_ID="aurora-gloriosa-cortes"
APP_BUILD="2026.10.07.3"
UPLOADS={}
UPLOADS_LOCK=threading.Lock()
PIPELINE_WAKE=threading.Event()

class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address=True


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream=super()._open()
        try: os.chmod(self.baseFilename,0o600)
        except OSError: pass
        return stream


def update_upload(upload_id, *, received=None, state=None, message=None):
    now=time.time()
    with UPLOADS_LOCK:
        item=UPLOADS.get(upload_id)
        if not item: return
        if received is not None: item["received_bytes"]=received
        if state is not None: item["state"]=state
        if message is not None: item["message"]=message
        item["updated_at"]=now


def upload_snapshot():
    now=time.time()
    with UPLOADS_LOCK:
        for key,item in list(UPLOADS.items()):
            if item["state"]!="receiving" and now-item["updated_at"]>1800:
                del UPLOADS[key]
        result=[]
        for item in sorted(UPLOADS.values(),key=lambda x:x["started_at"],reverse=True)[:10]:
            elapsed=max(0.1,now-item["started_at"])
            received=item["received_bytes"]
            total=item["total_bytes"]
            speed=received/elapsed
            result.append({"id":item["id"],"filename":item["filename"],"state":item["state"],
                "received_bytes":received,"total_bytes":total,
                "percent":min(100,round(100*received/total)) if total else 0,
                "bytes_per_second":round(speed),"eta_seconds":round(max(0,total-received)/speed) if speed>0 and item["state"]=="receiving" else None,
                "elapsed_seconds":round(elapsed),"message":item["message"],
                "updated_at":datetime.fromtimestamp(item["updated_at"],timezone.utc).isoformat()})
        return result


def cleanup_abandoned_uploads(max_age=72*3600):
    """Remove stale temporary uploads left by a process crash, never recent files."""
    cutoff=time.time()-max_age
    removed=[]
    for item in pipeline.INCOMING.glob(".*.part"):
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed.append(item.name)
        except OSError as exc:
            logging.warning("Não consegui limpar envio temporário %s: %s",item.name,exc)
    return removed


def configure_logging():
    """Keep a bounded, UTF-8 application log alongside the local job state."""
    log_path=pipeline.STATE/"aurora.log"
    log_path.parent.mkdir(parents=True,exist_ok=True)
    root_logger=logging.getLogger()
    if not any(getattr(handler,"_aurora_log_file",False) for handler in root_logger.handlers):
        file_handler=PrivateRotatingFileHandler(log_path,maxBytes=5*1024*1024,backupCount=4,encoding="utf-8")
        file_handler._aurora_log_file=True
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(threadName)s: %(message)s"))
        root_logger.addHandler(file_handler)
    return log_path


def _lock_instance():
    path=pipeline.STATE/"web.lock"
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open("a+")
    try:
        if os.name=="nt":
            import msvcrt
            handle.seek(0); handle.write(" "); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except (OSError,BlockingIOError):
        handle.seek(0)
        try: info=json.loads(handle.read() or "{}")
        except json.JSONDecodeError: info={}
        handle.close()
        return None,info
    return handle,{}


def _write_lock_info(handle,port):
    handle.seek(0); handle.truncate()
    handle.write(json.dumps({"pid":os.getpid(),"port":port,"app_id":APP_ID,"build":APP_BUILD}))
    handle.flush()


def _is_aurora_on_port(port):
    try:
        request=urllib.request.Request(f"http://127.0.0.1:{port}/api/status")
        with urllib.request.urlopen(request,timeout=1.5) as response: data=json.loads(response.read().decode("utf-8"))
        return data.get("app_id")==APP_ID or all(k in data for k in ("clips","mobile_url","progress"))
    except Exception:
        return False


def run_pipeline():
    while True:
        PIPELINE_WAKE.clear()
        try: pipeline.cycle_once()
        except Exception as exc:
            logging.exception("Erro no ciclo da automação")
            pipeline.set_progress("error", "", friendly_error(str(exc)), 0)
        PIPELINE_WAKE.wait(60)

def friendly_error(error):
    value=str(error)
    checks=[
        (("ollama","connection refused","urlopen error"),"A IA local não iniciou. Execute novamente o instalador da AURORA no computador para reparar tudo automaticamente."),
        (("youtube_client_secret.json","credenciais","oauth"),"O YouTube ainda não está autorizado. Confira as credenciais OAuth no tutorial e autorize seu canal."),
        (("instagram","meta","token"),"O Instagram não aceitou as credenciais. Confira o ID da conta profissional, o token e as permissões da Meta."),
        (("tiktok","video.publish","creator_info"),"O TikTok não aceitou a conexão. Confira a autorização video.publish e atualize o token."),
        (("ffmpeg","ffmpeg"),"Não consegui preparar o vídeo. Verifique se o arquivo abre normalmente e se há espaço livre no computador."),
        (("429","quota","rate limit"),"A plataforma limitou temporariamente os envios. A Aurora tentará novamente mais tarde."),
        (("permissão","permission denied","acesso negado"),"O computador bloqueou o acesso a um arquivo. Escolha uma pasta onde você tenha permissão de gravação."),
    ]
    low=value.casefold()
    for terms,message in checks:
        if any(term in low for term in terms): return message
    return value[:1000]

def lan_ip():
    probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1",80))
        return probe.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        probe.close()


class Handler(BaseHTTPRequestHandler):
    server_version="ClipperLocalUI/2.0"

    def log_message(self,fmt,*args): logging.info("web %s - %s",self.address_string(),fmt % args)

    def send_json(self,payload,status=200,headers=None):
        raw=json.dumps(payload,ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store")
        for key,value in (headers or {}).items(): self.send_header(key,value)
        self.end_headers(); self.wfile.write(raw)

    def read_json(self):
        length=int(self.headers.get("Content-Length","0"))
        if length>32000: raise ValueError("Solicitação muito grande.")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path=urlsplit(self.path)
        if path.path=="/":
            raw=(WEB/"index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        elif path.path=="/tutorial":
            raw=(WEB/"tutorial.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Content-Length",str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        elif path.path=="/conectar":
            raw=(WEB/"connect.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Content-Length",str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        elif path.path=="/doar":
            raw=(WEB/"donate.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Content-Length",str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        elif path.path=="/api/health":
            host=lan_ip() if HOST in {"0.0.0.0",""} else HOST
            self.send_json({"app_id":APP_ID,"build":APP_BUILD,"url":f"http://{host}:{PORT}"})
        elif path.path=="/api/uploads":
            self.send_json({"uploads":upload_snapshot()})
        elif path.path=="/api/status":
            try:
                cfg=pipeline.config()
                with pipeline.db_conn() as con:
                    rows=con.execute("SELECT id,path,status,topic,scope,layout,title,description,hashtags,scheduled_at,last_error,youtube_id FROM clips ORDER BY id DESC LIMIT 30").fetchall()
                    social={r[0]:con.execute("SELECT platform,status,platform_id,last_error,approved FROM platform_posts WHERE clip_id=?",(r[0],)).fetchall() for r in rows}
                    tests={r[0]:con.execute("SELECT id,status,platform_id,error,created_at FROM youtube_tests WHERE clip_id=? ORDER BY id DESC LIMIT 1",(r[0],)).fetchone() for r in rows}
                clips=[{"id":r[0],"file":Path(r[1]).name,"status":r[2],"topic":r[3],"scope":r[4],"layout":r[5],"title":r[6],"description":r[7],"hashtags":json.loads(r[8] or "[]"),"scheduled_at":r[9],"last_error":r[10],"youtube_id":r[11],"test":{"id":tests[r[0]][0],"status":tests[r[0]][1],"id_on_platform":tests[r[0]][2],"error":tests[r[0]][3],"created_at":tests[r[0]][4]} if tests[r[0]] else None,"platforms":[{"name":p[0],"status":p[1],"id":p[2],"error":p[3],"approved":bool(p[4])} for p in social[r[0]]]} for r in rows]
                secrets=pipeline.social_secrets()
                connected={"youtube":(ROOT/"token.json").exists() and (ROOT/"youtube_client_secret.json").exists(),"instagram":bool(secrets.get("instagram_user_id") and secrets.get("instagram_access_token")),"tiktok":bool(secrets.get("tiktok_access_token"))}
                youtube_setup={"credentials_uploaded":(ROOT/"youtube_client_secret.json").exists(),"authorized":(ROOT/"token.json").exists()}
                host=lan_ip() if HOST in {"0.0.0.0",""} else HOST
                self.send_json({"app_id":APP_ID,"build":APP_BUILD,"config":cfg,"ai_setup":{"api_key_configured":bool(pipeline.ai_secrets().get("api_key"))},"clips":clips,"connected":connected,"youtube_setup":youtube_setup,"social_setup":{"instagram_user_id":secrets.get("instagram_user_id","")},"progress":pipeline.get_progress(),"uploads":upload_snapshot(),"mobile_url":f"http://{host}:{PORT}","input_folder":str(pipeline.INCOMING),"output_folder":str(Path(cfg.get("output_folder") or pipeline.OUTPUT).expanduser())})
            except SystemExit as exc:
                logging.error("Configuração necessária para o painel ausente: %s",exc)
                self.send_json({"error":friendly_error(str(exc))},500)
            except Exception as exc:
                logging.exception("Falha ao carregar o estado do painel")
                self.send_json({"error":friendly_error(str(exc))},500)
        elif path.path=="/api/health/ollama":
            try:
                req=urllib.request.Request(os.getenv("OLLAMA_BASE_URL","http://127.0.0.1:11434").rstrip("/")+"/api/tags")
                with urllib.request.urlopen(req,timeout=3) as response: data=json.loads(response.read().decode("utf-8"))
                models=[item.get("name","") for item in data.get("models",[])]
                ready=any(name.startswith(pipeline.config().get("ollama_model","qwen3:8b")) for name in models)
                self.send_json({"running":True,"model_ready":ready,"models":models,"message":"Ollama está aberto e o modelo da Aurora está pronto." if ready else "Ollama está aberto, mas o modelo qwen3:8b ainda precisa ser baixado."})
            except Exception:
                self.send_json({"running":False,"model_ready":False,"models":[],"message":"Não encontrei o Ollama aberto neste computador."})
        elif path.path=="/api/diagnostics":
            try:
                cfg={}
                config_path=ROOT/"config.json"
                if config_path.exists():
                    try: cfg=pipeline.config()
                    except Exception as exc: config_error=str(exc)
                    else: config_error=""
                else: config_error="Arquivo config.json não encontrado. Execute o instalador ou restaure sua configuração."
                model=cfg.get("ollama_model","qwen3:8b")
                ollama={"ok":False,"message":"Ollama não respondeu no endereço local.","models":[]}
                try:
                    req=urllib.request.Request(os.getenv("OLLAMA_BASE_URL","http://127.0.0.1:11434").rstrip("/")+"/api/tags")
                    with urllib.request.urlopen(req,timeout=2.5) as response: data=json.loads(response.read().decode("utf-8"))
                    models=[item.get("name","") for item in data.get("models",[]) if item.get("name")]
                    model_ready=any(name.startswith(model) for name in models)
                    ollama={"ok":model_ready,"running":True,"model_ready":model_ready,"model":model,"models":models,
                        "message":f"Modelo {model} pronto." if model_ready else f"Ollama está aberto, mas o modelo {model} não foi encontrado."}
                except Exception as exc: ollama["detail"]=str(exc)
                ffmpeg={"ok":False,"message":"FFmpeg não foi encontrado."}
                try:
                    binary=pipeline.ffmpeg_binary()
                    result=subprocess.run([binary,"-version"],capture_output=True,text=True,timeout=4)
                    ffmpeg={"ok":result.returncode==0,"message":"FFmpeg pronto." if result.returncode==0 else "FFmpeg foi encontrado, mas não iniciou.","version":(result.stdout or result.stderr).splitlines()[0] if result.stdout or result.stderr else binary}
                except Exception as exc: ffmpeg["detail"]=str(exc)
                output=Path(cfg.get("output_folder") or pipeline.OUTPUT).expanduser()
                folders=[]
                for label,folder in (("Entrada de vídeos",pipeline.INCOMING),("Saída dos cortes",output),("Estado e fila",pipeline.STATE)):
                    try: ready=folder.is_dir() and os.access(folder,os.W_OK)
                    except OSError: ready=False
                    folders.append({"name":label,"ok":ready,"path":str(folder),"message":"Disponível para gravação." if ready else "Pasta ausente ou sem permissão de gravação.","detail":"Caminho: "+str(folder)})
                try:
                    usage=shutil.disk_usage(output if output.exists() else output.parent)
                    disk={"ok":usage.free>256*1024**2,"free_bytes":usage.free,"message":f"{usage.free/(1024**3):.1f} GB livres." if usage.free>256*1024**2 else "Menos de 256 MB livres no disco de saída."}
                except OSError as exc: disk={"ok":False,"message":"Não consegui verificar o espaço em disco.","detail":str(exc)}
                whisper_ready=importlib.util.find_spec("faster_whisper") is not None
                try:
                    av_version=importlib.metadata.version("av")
                    av_compatible=int(av_version.split(".",1)[0])<19
                    av_message=f"PyAV {av_version} instalado." if av_compatible else f"PyAV {av_version} é incompatível com a versão atual do faster-whisper; o PyAV precisa ser anterior à versão 19."
                except importlib.metadata.PackageNotFoundError:
                    av_version="não instalado"; av_compatible=False; av_message="PyAV não está instalado neste ambiente."
                progress=pipeline.get_progress()
                progress_age=None
                try: progress_age=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(progress.get("updated_at","").replace("Z","+00:00"))).total_seconds()))
                except (ValueError,TypeError): pass
                checks=[{"name":"Configuração","ok":not config_error,"message":config_error or "Configuração carregada.","detail":config_error or str(config_path)},
                    {"name":"Transcrição local","ok":whisper_ready,"message":"faster-whisper instalado." if whisper_ready else "faster-whisper não está instalado neste ambiente."},
                    {"name":"Decoder de áudio PyAV","ok":av_compatible,"message":av_message,"detail":f"Versão instalada: {av_version}; requisito do projeto: av>=11,<19."},
                    {"name":"Ollama e modelo","ok":ollama["ok"],"message":ollama["message"],"detail":("Modelos instalados: "+", ".join(ollama.get("models",[]))) if ollama.get("models") else ollama.get("detail")},
                    {"name":"FFmpeg","ok":ffmpeg["ok"],"message":ffmpeg["message"],"detail":ffmpeg.get("version") or ffmpeg.get("detail")},
                    {"name":"Espaço em disco","ok":disk["ok"],"message":disk["message"],"detail":disk.get("detail")},*folders]
                log_path=pipeline.STATE/"aurora.log"
                checks.append({"name":"Arquivo de log","ok":log_path.exists() and os.access(log_path,os.W_OK),
                    "message":"Log geral ativo." if log_path.exists() else "O arquivo de log ainda não foi criado.","detail":str(log_path)})
                self.send_json({"checks":checks,"ollama":ollama,"ffmpeg":ffmpeg,"disk":disk,"folders":folders,"log_file":str(log_path),
                    "progress":{"stage":progress.get("stage","idle"),"message":progress.get("message","Aguardando atividade."),"file":progress.get("file",""),"age_seconds":progress_age}})
            except Exception as exc:
                logging.exception("Falha ao montar diagnóstico do computador")
                self.send_json({"error":friendly_error(str(exc)),"detail":str(exc)},500)
        elif path.path=="/api/mobile-qr.svg":
            try:
                import qrcode
                from qrcode.image.svg import SvgPathImage
                host=lan_ip() if HOST in {"0.0.0.0",""} else HOST
                qr=qrcode.make(f"http://{host}:{PORT}",image_factory=SvgPathImage,box_size=8,border=3)
                from io import BytesIO
                output=BytesIO(); qr.save(output)
                raw=output.getvalue(); self.send_response(200); self.send_header("Content-Type","image/svg+xml")
                self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store")
                self.end_headers(); self.wfile.write(raw)
            except ModuleNotFoundError:
                raw=b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180"><rect width="320" height="180" rx="12" fill="white"/><text x="160" y="82" text-anchor="middle" font-family="sans-serif" font-size="18" fill="#222">QR nao disponivel</text><text x="160" y="112" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#555">use o endereco local ao lado</text></svg>'
                self.send_response(200); self.send_header("Content-Type","image/svg+xml"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
            except Exception as exc: self.send_json({"error":friendly_error(str(exc))},500)
        elif path.path=="/api/folders":
            try:
                requested=parse_qs(path.query).get("path",[""])[0]
                if requested:
                    folder=Path(requested).expanduser().resolve()
                    if not folder.is_dir(): raise ValueError("Essa pasta não existe ou não pode ser aberta.")
                    folders=[]
                    for item in sorted(folder.iterdir(),key=lambda p:(not p.is_dir(),p.name.casefold())):
                        try:
                            if item.is_dir() and not item.name.startswith("."):
                                folders.append({"name":item.name,"path":str(item.resolve())})
                        except (PermissionError,OSError): pass
                    parent=str(folder.parent) if folder.parent!=folder else None
                    self.send_json({"current":str(folder),"parent":parent,"folders":folders})
                else:
                    roots=[]
                    if sys.platform=="win32":
                        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                            drive=Path(f"{letter}:\\")
                            if drive.exists(): roots.append({"name":str(drive),"path":str(drive)})
                    else:
                        roots=[{"name":"Pasta pessoal","path":str(Path.home())},{"name":"Sistema de arquivos","path":"/"}]
                    self.send_json({"current":"","parent":None,"folders":roots})
            except (ValueError,OSError) as exc: self.send_json({"error":str(exc)},400)
        else: self.send_error(404)

    def do_POST(self):
        try:
            route=urlsplit(self.path).path
            body=self.read_json()
            if route=="/api/config": return self.save_config(body)
            if route=="/api/test-ai":
                cfg=pipeline.config()
                if cfg.get("ai_provider","local")!="external": raise ValueError("Selecione e salve primeiro o provedor externo de IA.")
                client,model=pipeline.analysis_ai_client(cfg)
                schema={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":False}
                response=client.chat.completions.create(model=model,messages=[{"role":"user","content":"Retorne um objeto JSON com ok=true."}],response_format={"type":"json_schema","json_schema":{"name":"connection_check","strict":True,"schema":schema}},max_tokens=20)
                if not response.choices or not response.choices[0].message.content: raise RuntimeError("O provedor respondeu sem retornar uma mensagem.")
                if not json.loads(response.choices[0].message.content).get("ok"): raise RuntimeError("O provedor respondeu, mas não confirmou a saída JSON esperada pela Aurora.")
                self.send_json({"ok":True,"message":f"Provedor de IA conectado. Modelo informado: {model}."}); return
            if route=="/api/client-error":
                message=str(body.get("message","Erro sem mensagem"))[:1200]
                source=str(body.get("source","painel"))[:80]
                page=str(body.get("page","/"))[:160]
                stack=str(body.get("stack",""))[:4000]
                logging.error("Erro de interface no painel (%s, %s): %s",source,page,message)
                if stack: logging.error("Stack do navegador: %s",stack)
                self.send_json({"ok":True},202); return
            if route=="/api/credentials/social":
                secrets=pipeline.social_secrets()
                for name in ("instagram_user_id","instagram_access_token","tiktok_access_token"):
                    value=str(body.get(name,"")).strip()
                    if value: secrets[name]=value
                has_ig_id=bool(secrets.get("instagram_user_id")); has_ig_token=bool(secrets.get("instagram_access_token")); has_tt_token=bool(secrets.get("tiktok_access_token"))
                if has_ig_id != has_ig_token: raise ValueError("Para conectar Instagram, preencha o ID profissional e o token Meta juntos.")
                if not has_ig_id and not has_tt_token: raise ValueError("Preencha as credenciais de ao menos uma rede antes de salvar.")
                secret_path=ROOT/"social_secrets.json"
                secret_path.write_text(json.dumps(secrets,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                try: secret_path.chmod(0o600)
                except OSError: pass
                self.send_json({"ok":True,"message":"Credenciais salvas neste computador. Agora use “Testar conexão” para validá-las."}); return
            if route=="/api/credentials/youtube":
                client=body.get("client_secret")
                if not isinstance(client,dict) or not isinstance(client.get("installed"),dict):
                    raise ValueError("Esse não parece ser o arquivo OAuth de aplicativo para computador. No Google Cloud, crie “ID do cliente OAuth” do tipo “App para computador” e selecione o arquivo JSON baixado.")
                installed=client["installed"]
                if not all(installed.get(key) for key in ("client_id","client_secret","auth_uri","token_uri")):
                    raise ValueError("O arquivo OAuth está incompleto. Baixe novamente as credenciais do tipo aplicativo para computador.")
                secret_path=ROOT/"youtube_client_secret.json"
                secret_path.write_text(json.dumps(client,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                try: secret_path.chmod(0o600)
                except OSError: pass
                self.send_json({"ok":True,"message":"Arquivo do YouTube recebido com segurança. Agora autorize o canal usando o botão abaixo."}); return
            if route=="/api/approve":
                clip_id=int(body.get("clip_id",0)); cfg=pipeline.config(); platforms=cfg.get("publish_platforms",["youtube"])
                with pipeline.db_conn() as con:
                    clip=con.execute("SELECT id FROM clips WHERE id=?",(clip_id,)).fetchone()
                    if not clip: raise ValueError("Esse corte não está mais na fila.")
                    for platform in platforms:
                        con.execute("INSERT OR IGNORE INTO platform_posts(clip_id,platform,status,approved) VALUES(?,?, 'ready',0)",(clip_id,platform))
                        con.execute("UPDATE platform_posts SET approved=1,next_attempt_at=NULL,last_error=NULL WHERE clip_id=? AND platform=? AND status IN ('ready','failed')",(clip_id,platform))
                PIPELINE_WAKE.set()
                self.send_json({"ok":True,"message":"Corte liberado. Ele seguirá o intervalo configurado."}); return
            if route=="/api/test-private":
                clip_id=int(body.get("clip_id",0))
                with pipeline.db_conn() as con:
                    row=con.execute("SELECT id,path,topic,scope,title,description,hashtags FROM clips WHERE id=?",(clip_id,)).fetchone()
                    if not row or not Path(row[1]).is_file(): raise ValueError("O arquivo do corte não foi encontrado.")
                    running=con.execute("SELECT 1 FROM youtube_tests WHERE clip_id=? AND status='uploading'",(clip_id,)).fetchone()
                    if running: raise ValueError("O teste deste corte já está sendo enviado.")
                    cur=con.execute("INSERT INTO youtube_tests(clip_id,status,created_at) VALUES(?,'uploading',?)",(clip_id,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())))
                    test_id=cur.lastrowid
                threading.Thread(target=self.run_private_test,args=(test_id,row),daemon=True).start()
                self.send_json({"ok":True,"message":"O teste privado foi iniciado. A primeira autorização do YouTube pode abrir uma janela do navegador."},202); return
            if route=="/api/test-connection":
                platform=body.get("platform")
                if platform=="youtube":
                    if not (ROOT/"youtube_client_secret.json").exists(): raise ValueError("Adicione o arquivo de credenciais do YouTube conforme o tutorial.")
                    pipeline.youtube_client()
                    detail="YouTube autorizado neste computador."
                elif platform=="instagram":
                    secrets=pipeline.social_secrets(); user_id=secrets.get("instagram_user_id","").strip(); token=secrets.get("instagram_access_token","").strip()
                    if not user_id or not token: raise ValueError("Abra a seção de conexão, informe o ID profissional e o token Meta e salve antes de testar.")
                    user=pipeline._social_request(f"https://graph.facebook.com/{pipeline.config().get('instagram_api_version','v26.0')}/{user_id}?fields=id,username",token=token)
                    detail="Instagram conectado como @"+user.get("username",user_id)
                elif platform=="tiktok":
                    secrets=pipeline.social_secrets(); token=secrets.get("tiktok_access_token","")
                    if not token: raise ValueError("Informe e salve o token TikTok com a permissão video.publish antes de testar.")
                    result=pipeline._social_request("https://open.tiktokapis.com/v2/post/publish/creator_info/query/",token=token,body={},method="POST")
                    detail="TikTok autorizado. Privacidades disponíveis: "+", ".join(result.get("data",{}).get("privacy_level_options",[]))
                else: raise ValueError("Escolha uma rede válida para testar.")
                self.send_json({"ok":True,"message":detail}); return
            if route!="/api/config": self.send_error(404); return
            return self.save_config(body)
        except (ValueError,json.JSONDecodeError) as exc:
            logging.warning("Solicitação rejeitada em %s: %s",route,exc)
            self.send_json({"error":friendly_error(str(exc))},400)
        except Exception as exc:
            logging.exception("Falha ao processar solicitação em %s",route)
            self.send_json({"error":friendly_error(str(exc))},500)

    def save_config(self,body):
        try:
            cfg=pipeline.config()
            interval=float(body.get("posting_interval_hours",6))
            if not 0.25<=interval<=720: raise ValueError("O intervalo deve ficar entre 0,25 e 720 horas.")
            privacy=body.get("youtube_privacy","private")
            if privacy not in {"private","unlisted","public"}: raise ValueError("Visibilidade inválida.")
            layout=body.get("video_layout","vertical_center")
            strategy=body.get("hashtag_strategy","balanced")
            mode=body.get("posting_mode","scheduled")
            ai_provider=body.get("ai_provider",cfg.get("ai_provider","local"))
            external_ai_base_url=str(body.get("external_ai_base_url",cfg.get("external_ai_base_url",""))).strip().rstrip("/")
            external_ai_model=str(body.get("external_ai_model",cfg.get("external_ai_model",""))).strip()
            if ai_provider not in {"local","external"}: raise ValueError("Escolha a IA local ou um provedor externo.")
            if ai_provider=="external":
                from urllib.parse import urlsplit
                endpoint=urlsplit(external_ai_base_url)
                local_http=endpoint.scheme=="http" and endpoint.hostname in {"localhost","127.0.0.1","::1"}
                if not endpoint.hostname or (endpoint.scheme!="https" and not local_http) or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                    raise ValueError("Use um endereço HTTPS do provedor (HTTP só é aceito para localhost).")
                if not external_ai_model: raise ValueError("Informe o nome do modelo do provedor externo.")
            api_key=str(body.get("ai_api_key","")).strip()
            clear_api_key=bool(body.get("clear_ai_api_key",False))
            stored_ai_key=str(pipeline.ai_secrets().get("api_key","")).strip()
            if clear_api_key: stored_ai_key=""
            if ai_provider=="external" and not (api_key or stored_ai_key): raise ValueError("Informe a chave da API do provedor externo.")
            if layout not in pipeline.VIDEO_LAYOUTS: raise ValueError("Layout inválido.")
            if strategy not in pipeline.HASHTAG_STRATEGIES: raise ValueError("Estratégia de hashtag inválida.")
            if mode not in pipeline.POSTING_MODES: raise ValueError("Modo de postagem inválido.")
            platforms=body.get("publish_platforms",["youtube"])
            if not isinstance(platforms,list) or not platforms or len(set(platforms))!=len(platforms) or set(platforms)-{"youtube","instagram","tiktok"}:
                raise ValueError("Escolha ao menos uma rede social válida.")
            if len(platforms)>1 and mode=="scheduled": raise ValueError("Para publicar em várias redes na mesma rodada, escolha o modo de postagem imediato.")
            output=Path(body.get("output_folder") or pipeline.OUTPUT).expanduser().resolve()
            output.mkdir(parents=True,exist_ok=True)
            cfg.update({"posting_interval_hours":interval,"auto_publish":bool(body.get("auto_publish",False)),"pipeline_paused":bool(body.get("pipeline_paused",cfg.get("pipeline_paused",False))),"youtube_privacy":privacy,"video_layout":layout,"hashtag_strategy":strategy,"posting_mode":mode,"publish_platforms":platforms,"output_folder":str(output),"ai_provider":ai_provider,"external_ai_base_url":external_ai_base_url,"external_ai_model":external_ai_model})
            ai_secret_path=ROOT/"ai_secrets.json"
            if api_key:
                ai_secret_path.write_text(json.dumps({"api_key":api_key},indent=2)+"\n",encoding="utf-8")
                try: ai_secret_path.chmod(0o600)
                except OSError: pass
            elif clear_api_key and ai_secret_path.exists():
                ai_secret_path.unlink()
            secrets=pipeline.social_secrets()
            for name in ("instagram_user_id","instagram_access_token","tiktok_access_token"):
                value=str(body.get(name,"")).strip()
                if value: secrets[name]=value
            if secrets:
                secret_path=ROOT/"social_secrets.json"
                secret_path.write_text(json.dumps(secrets,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                try: secret_path.chmod(0o600)
                except OSError: pass
            if bool(cfg.get("auto_publish")) != bool(body.get("auto_publish",False)):
                with pipeline.db_conn() as con:
                    if body.get("auto_publish",False): con.execute("UPDATE platform_posts SET approved=1 WHERE status IN ('ready','failed')")
                    else: con.execute("UPDATE platform_posts SET approved=0 WHERE status='ready'")
            cfg.pop("topics",None)
            (ROOT/"config.json").write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            PIPELINE_WAKE.set()
            self.send_json({"ok":True,"message":"Configurações salvas."})
        except (ValueError,json.JSONDecodeError) as exc:
            logging.warning("Configuração rejeitada: %s",exc)
            self.send_json({"error":friendly_error(str(exc))},400)
        except Exception as exc:
            logging.exception("Falha ao salvar configuração")
            self.send_json({"error":friendly_error(str(exc))},500)

    def run_private_test(self,test_id,row):
        try:
            cfg={**pipeline.config(),"posting_mode":"immediate","youtube_privacy":"private"}
            platform_id,_,_=pipeline.publish_one("youtube",row,cfg,pipeline.social_secrets(),False)
            with pipeline.db_conn() as con: con.execute("UPDATE youtube_tests SET status='private',platform_id=?,error=NULL WHERE id=?",(platform_id,test_id))
        except Exception as exc:
            logging.exception("Falha ao enviar teste privado do corte %s",row[0])
            with pipeline.db_conn() as con: con.execute("UPDATE youtube_tests SET status='failed',error=? WHERE id=?",(friendly_error(str(exc)),test_id))

    def do_PUT(self):
        temp=None; upload_id=None
        try:
            if urlsplit(self.path).path!="/api/upload": self.send_error(404); return
            length=int(self.headers.get("Content-Length","0"))
            if length<=0: raise ValueError("O arquivo selecionado está vazio.")
            if length>MAX_UPLOAD_BYTES: raise ValueError("O limite por arquivo é 8 GB.")
            raw_name=unquote(self.headers.get("X-File-Name","video.mp4"))
            filename=Path(raw_name.replace("\\","/")).name
            filename=re.sub(r"[^A-Za-z0-9._-]+","_",filename).strip("._")[:120]
            if not filename or Path(filename).suffix.lower() not in pipeline.VIDEO_EXTS:
                raise ValueError("Formato não aceito. Use MP4, MOV, MKV, WEBM ou M4V.")
            pipeline.INCOMING.mkdir(parents=True,exist_ok=True)
            upload_id=uuid.uuid4().hex
            now=time.time()
            with UPLOADS_LOCK:
                for key,item in list(UPLOADS.items()):
                    if item["state"]!="receiving" and now-item["updated_at"]>1800: del UPLOADS[key]
                UPLOADS[upload_id]={"id":upload_id,"filename":filename,"state":"receiving","received_bytes":0,
                    "total_bytes":length,"started_at":now,"updated_at":now,"message":"Conectando e preparando o envio."}
            temp=pipeline.INCOMING/("."+uuid.uuid4().hex+".part")
            remaining=length
            with temp.open("wb") as out:
                while remaining:
                    block=self.rfile.read(min(4*1024*1024,remaining))
                    if not block: raise ValueError("O envio do arquivo foi interrompido.")
                    out.write(block); remaining-=len(block)
                    received=length-remaining
                    update_upload(upload_id,received=received,message=f"Recebendo no computador · {round(100*received/length)}%")
            target=pipeline.INCOMING/(uuid.uuid4().hex[:10]+"_"+filename)
            os.replace(temp,target); temp=None
            update_upload(upload_id,state="received",received=length,message="Arquivo salvo. A análise foi adicionada à fila.")
            PIPELINE_WAKE.set()
            self.send_json({"ok":True,"file":target.name,"message":f"{filename} recebido e adicionado à fila."},201)
        except (ValueError,OverflowError) as exc:
            logging.warning("Envio rejeitado%s: %s",f" ({upload_id})" if upload_id else "",exc)
            if upload_id: update_upload(upload_id,state="failed",message=str(exc))
            self.send_json({"error":str(exc)},400)
        except Exception as exc:
            logging.exception("Falha ao receber vídeo%s",f" ({upload_id})" if upload_id else "")
            if upload_id: update_upload(upload_id,state="failed",message=f"Falha ao salvar o vídeo: {exc}")
            self.send_json({"error":str(exc)},500)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
                if upload_id: update_upload(upload_id,state="failed",message="Envio incompleto; arquivo temporário removido.")


def main():
    global PORT
    for folder in (pipeline.INCOMING,pipeline.OUTPUT,pipeline.STATE): folder.mkdir(parents=True,exist_ok=True)
    cleanup_abandoned_uploads()
    lock,info=_lock_instance()
    if lock is None:
        port=int(info.get("port",PORT) or PORT)
        if info.get("app_id")==APP_ID or _is_aurora_on_port(port):
            url=f"http://127.0.0.1:{port}"
            logging.info("A AURORA já está em execução: %s. Mantive a instância existente.",url)
            if os.getenv("AURORA_OPEN_BROWSER")=="1": webbrowser.open(url)
        else:
            logging.info("A AURORA já está iniciando. Aguarde alguns segundos e abra http://127.0.0.1:%d",port)
        return
    log_path=configure_logging()
    logging.info("Log geral ativo em %s",log_path)
    server=None
    try:
        try:
            server=ReusableHTTPServer((HOST,PORT),Handler)
        except OSError as exc:
            if exc.errno!=errno.EADDRINUSE: raise
            if _is_aurora_on_port(PORT):
                logging.error("Uma versão anterior da AURORA já ocupa a porta %d. Feche a janela do processo anterior com Ctrl+C e execute o iniciador novamente; evitei iniciar uma segunda fila de processamento.",PORT)
                return
            requested=PORT
            for candidate in range(requested+1,requested+26):
                try:
                    server=ReusableHTTPServer((HOST,candidate),Handler); PORT=candidate; break
                except OSError as candidate_error:
                    if candidate_error.errno!=errno.EADDRINUSE: raise
            if server is None: raise OSError(errno.EADDRINUSE,"Não encontrei uma porta livre entre %d e %d."%(requested,requested+25))
            logging.warning("A porta %d estava ocupada por outro programa. A Aurora abriu na porta %d.",requested,PORT)
        _write_lock_info(lock,PORT)
        logging.info("Interface local: http://127.0.0.1:%d",PORT)
        if HOST in {"0.0.0.0",""}:
            try: logging.info("Acesso pela mesma rede Wi-Fi: http://%s:%d",lan_ip(),PORT)
            except OSError: pass
        threading.Thread(target=run_pipeline,daemon=True,name="clipper-pipeline").start()
        if os.getenv("AURORA_OPEN_BROWSER")=="1": webbrowser.open(f"http://127.0.0.1:{PORT}")
        try: server.serve_forever()
        except KeyboardInterrupt: pass
    except OSError as exc:
        logging.error("A interface não iniciou: %s",exc)
    finally:
        if server is not None: server.server_close()
        lock.close()

if __name__=="__main__": main()
