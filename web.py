#!/usr/bin/env python3
"""Local browser UI for the authorized video clipping pipeline."""
from __future__ import annotations
import json, logging, os, re, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs
import socket, sys, urllib.request
import main as pipeline

ROOT=Path(__file__).resolve().parent
WEB=ROOT/"web"
PORT=int(os.getenv("CLIPPER_PORT","8774"))
HOST=os.getenv("CLIPPER_HOST","0.0.0.0")
MAX_UPLOAD_BYTES=8*1024*1024*1024

class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address=True


def run_pipeline():
    while True:
        try: pipeline.cycle_once()
        except Exception as exc:
            logging.exception("Erro no ciclo da automação")
            pipeline.set_progress("error", "", friendly_error(str(exc)), 0)
        time.sleep(60)

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
            self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Content-Length",str(len(raw)))
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
                self.send_json({"config":cfg,"clips":clips,"connected":connected,"youtube_setup":youtube_setup,"social_setup":{"instagram_user_id":secrets.get("instagram_user_id","")},"progress":pipeline.get_progress(),"mobile_url":f"http://{host}:{PORT}","input_folder":str(pipeline.INCOMING),"output_folder":str(Path(cfg.get("output_folder") or pipeline.OUTPUT).expanduser())})
            except Exception as exc: self.send_json({"error":str(exc)},500)
        elif path.path=="/api/health/ollama":
            try:
                req=urllib.request.Request(os.getenv("OLLAMA_BASE_URL","http://127.0.0.1:11434").rstrip("/")+"/api/tags")
                with urllib.request.urlopen(req,timeout=3) as response: data=json.loads(response.read().decode("utf-8"))
                models=[item.get("name","") for item in data.get("models",[])]
                ready=any(name.startswith(pipeline.config().get("ollama_model","qwen3:8b")) for name in models)
                self.send_json({"running":True,"model_ready":ready,"models":models,"message":"Ollama está aberto e o modelo da Aurora está pronto." if ready else "Ollama está aberto, mas o modelo qwen3:8b ainda precisa ser baixado."})
            except Exception:
                self.send_json({"running":False,"model_ready":False,"models":[],"message":"Não encontrei o Ollama aberto neste computador."})
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
        except (ValueError,json.JSONDecodeError) as exc: self.send_json({"error":friendly_error(str(exc))},400)
        except Exception as exc: self.send_json({"error":friendly_error(str(exc))},500)

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
            if layout not in pipeline.VIDEO_LAYOUTS: raise ValueError("Layout inválido.")
            if strategy not in pipeline.HASHTAG_STRATEGIES: raise ValueError("Estratégia de hashtag inválida.")
            if mode not in pipeline.POSTING_MODES: raise ValueError("Modo de postagem inválido.")
            platforms=body.get("publish_platforms",["youtube"])
            if not isinstance(platforms,list) or not platforms or len(set(platforms))!=len(platforms) or set(platforms)-{"youtube","instagram","tiktok"}:
                raise ValueError("Escolha ao menos uma rede social válida.")
            if len(platforms)>1 and mode=="scheduled": raise ValueError("Para publicar em várias redes na mesma rodada, escolha o modo de postagem imediato.")
            output=Path(body.get("output_folder") or pipeline.OUTPUT).expanduser().resolve()
            output.mkdir(parents=True,exist_ok=True)
            cfg.update({"posting_interval_hours":interval,"auto_publish":bool(body.get("auto_publish",False)),"pipeline_paused":bool(body.get("pipeline_paused",cfg.get("pipeline_paused",False))),"youtube_privacy":privacy,"video_layout":layout,"hashtag_strategy":strategy,"posting_mode":mode,"publish_platforms":platforms,"output_folder":str(output)})
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
            self.send_json({"ok":True,"message":"Configurações salvas."})
        except (ValueError,json.JSONDecodeError) as exc: self.send_json({"error":friendly_error(str(exc))},400)
        except Exception as exc: self.send_json({"error":friendly_error(str(exc))},500)

    def run_private_test(self,test_id,row):
        try:
            cfg={**pipeline.config(),"posting_mode":"immediate","youtube_privacy":"private"}
            platform_id,_,_=pipeline.publish_one("youtube",row,cfg,pipeline.social_secrets(),False)
            with pipeline.db_conn() as con: con.execute("UPDATE youtube_tests SET status='private',platform_id=?,error=NULL WHERE id=?",(platform_id,test_id))
        except Exception as exc:
            with pipeline.db_conn() as con: con.execute("UPDATE youtube_tests SET status='failed',error=? WHERE id=?",(friendly_error(str(exc)),test_id))

    def do_PUT(self):
        temp=None
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
            temp=pipeline.INCOMING/("."+uuid.uuid4().hex+".part")
            remaining=length
            with temp.open("wb") as out:
                while remaining:
                    block=self.rfile.read(min(1024*1024,remaining))
                    if not block: raise ValueError("O envio do arquivo foi interrompido.")
                    out.write(block); remaining-=len(block)
            target=pipeline.INCOMING/(uuid.uuid4().hex[:10]+"_"+filename)
            os.replace(temp,target); temp=None
            pipeline.set_progress("waiting",target.name,"Vídeo recebido. A análise vai começar em instantes.",0)
            self.send_json({"ok":True,"file":target.name,"message":f"{filename} recebido e adicionado à fila."},201)
        except (ValueError,OverflowError) as exc: self.send_json({"error":str(exc)},400)
        except Exception as exc: self.send_json({"error":str(exc)},500)
        finally:
            if temp is not None: temp.unlink(missing_ok=True)


def main():
    pipeline.INCOMING.mkdir(exist_ok=True); pipeline.OUTPUT.mkdir(exist_ok=True); pipeline.STATE.mkdir(exist_ok=True)
    threading.Thread(target=run_pipeline,daemon=True,name="clipper-pipeline").start()
    server=ReusableHTTPServer((HOST,PORT),Handler)
    logging.info("Interface local: http://127.0.0.1:%d",PORT)
    if HOST in {"0.0.0.0",""}:
        try: logging.info("Acesso pela mesma rede Wi-Fi: http://%s:%d",lan_ip(),PORT)
        except OSError: pass
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()
