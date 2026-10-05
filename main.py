#!/usr/bin/env python3
"""Local-first authorized video clipping and YouTube scheduling MVP."""
from __future__ import annotations
import json, logging, os, sqlite3, subprocess, time, re, shutil, math, urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent
INCOMING, OUTPUT, STATE = ROOT / "incoming", ROOT / "output", ROOT / "state"
DB = STATE / "jobs.sqlite3"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
VIDEO_LAYOUTS = {"vertical_center", "blurred_fit", "caption_focus"}
HASHTAG_STRATEGIES = {"balanced", "search", "trend_aware"}
POSTING_MODES = {"scheduled", "immediate"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_progress_lock=__import__("threading").Lock()


def set_progress(stage, filename="", message="", percent=0):
    """Write a small local status snapshot for the web dashboard."""
    STATE.mkdir(parents=True,exist_ok=True)
    payload={"stage":stage,"file":filename,"message":message,"percent":max(0,min(100,int(percent))),"updated_at":datetime.now(timezone.utc).isoformat()}
    with _progress_lock:
        (STATE/"pipeline_status.json").write_text(json.dumps(payload,ensure_ascii=False),encoding="utf-8")


def get_progress():
    try: return json.loads((STATE/"pipeline_status.json").read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return {"stage":"idle","file":"","message":"Aguardando atividade.","percent":0}


def config():
    p = ROOT / "config.json"
    if not p.exists():
        raise SystemExit("Crie config.json copiando config.example.json e ajuste as opções de processamento.")
    return json.loads(p.read_text(encoding="utf-8"))


def social_secrets():
    path=ROOT/"social_secrets.json"
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return {}


def db_conn():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS clips (
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
        path TEXT NOT NULL UNIQUE, topic TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
        layout TEXT NOT NULL DEFAULT 'vertical_center', youtube_id TEXT,
        title TEXT NOT NULL, description TEXT NOT NULL, hashtags TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'ready',
        created_at TEXT NOT NULL, published_at TEXT, scheduled_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT
    )""")
    columns={row[1] for row in con.execute("PRAGMA table_info(clips)")}
    if "hashtags" not in columns:
        con.execute("ALTER TABLE clips ADD COLUMN hashtags TEXT NOT NULL DEFAULT '[]'")
    if "topic" not in columns:
        con.execute("ALTER TABLE clips ADD COLUMN topic TEXT NOT NULL DEFAULT ''")
    if "scope" not in columns:
        con.execute("ALTER TABLE clips ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
    if "layout" not in columns:
        con.execute("ALTER TABLE clips ADD COLUMN layout TEXT NOT NULL DEFAULT 'vertical_center'")
    for name, declaration in (("youtube_id","TEXT"),("scheduled_at","TEXT"),("attempts","INTEGER NOT NULL DEFAULT 0"),("next_attempt_at","TEXT"),("last_error","TEXT")):
        if name not in columns: con.execute(f"ALTER TABLE clips ADD COLUMN {name} {declaration}")
    con.execute("""CREATE TABLE IF NOT EXISTS platform_posts (
        clip_id INTEGER NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready',
        platform_id TEXT, published_at TEXT, approved INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT, last_error TEXT, PRIMARY KEY(clip_id,platform))""")
    post_columns={row[1] for row in con.execute("PRAGMA table_info(platform_posts)")}
    if "approved" not in post_columns:
        con.execute("ALTER TABLE platform_posts ADD COLUMN approved INTEGER NOT NULL DEFAULT 0")
    con.execute("""CREATE TABLE IF NOT EXISTS youtube_tests (
        id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'uploading',
        platform_id TEXT, error TEXT, created_at TEXT NOT NULL)""")
    con.commit()
    return con


def already_processed(source: Path) -> bool:
    with db_conn() as con:
        return con.execute("SELECT 1 FROM clips WHERE source=? LIMIT 1", (str(source),)).fetchone() is not None


def transcribe(source: Path, cfg):
    from faster_whisper import WhisperModel
    model = WhisperModel(cfg.get("whisper_model", "small"), device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(source), language=cfg.get("language", "pt"), vad_filter=True, word_timestamps=True)
    result=[]
    for s in segments:
        text=s.text.strip()
        if not text: continue
        words=getattr(s,"words",None) or []
        start=float(words[0].start) if words else float(s.start)
        end=float(words[-1].end) if words else float(s.end)
        result.append({"start":start,"end":end,"text":text})
    return result


def candidate_windows(segments, cfg):
    """Create candidate cuts with boundaries anchored to speech segments."""
    minimum=float(cfg.get("min_clip_seconds",18)); maximum=float(cfg.get("max_clip_seconds",60))
    targets=sorted({min(maximum-1,max(minimum+2,35)), min(maximum-0.5,max(minimum+1,48))})
    duration=float(segments[-1]["end"])-float(segments[0]["start"])
    stride=max(12.0,duration/120.0)
    windows=[]; next_start=-1.0
    for i, first in enumerate(segments):
        start=float(first["start"])
        if start < next_start: continue
        for target in targets:
            # Choose the nearest complete speech segment boundary to the target length.
            choices=[k for k in range(i,len(segments)) if minimum <= float(segments[k]["end"])-start <= maximum]
            if not choices: continue
            j=min(choices,key=lambda k:abs((float(segments[k]["end"])-start)-target))
            end=float(segments[j]["end"])
            words=" ".join(s["text"] for s in segments[i:j+1]).strip()
            if len(words)<70: continue
            before=" ".join(s["text"] for s in segments[max(0,i-2):i])
            after=" ".join(s["text"] for s in segments[j+1:j+3])
            windows.append({"id":f"W{len(windows)+1:03d}","start":start,"end":end,"text":words[:1200],"context_before":before,"context_after":after})
            next_start=start+stride
            if len(windows)>=240: return windows
    return windows


def current_topic_trends(topics):
    """Return tag candidates found on recent, high-view YouTube videos for these topics."""
    api_key=os.getenv("YOUTUBE_API_KEY")
    if not api_key: return []
    cache=STATE/"trends.json"
    cache_key="|".join(topics[:3]).casefold()
    try:
        cached=json.loads(cache.read_text(encoding="utf-8"))
        age=time.time()-cached.get("updated",0)
        if cached.get("key")==cache_key and age<6*3600: return cached.get("terms",[])
    except (OSError,json.JSONDecodeError,AttributeError): pass
    try:
        from googleapiclient.discovery import build
        service=build("youtube","v3",developerKey=api_key,cache_discovery=False)
        from datetime import timedelta
        published=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat().replace("+00:00","Z")
        weights={}
        for topic in topics[:3]:
            found=service.search().list(part="id,snippet",q=topic,type="video",order="viewCount",publishedAfter=published,regionCode="BR",maxResults=10).execute()
            ids=[item.get("id",{}).get("videoId") for item in found.get("items",[]) if item.get("id",{}).get("videoId")]
            if not ids: continue
            videos=service.videos().list(part="snippet,statistics",id=",".join(ids),maxResults=50).execute()
            for video in videos.get("items",[]):
                views=max(1,int(video.get("statistics",{}).get("viewCount",1)))
                score=1+min(5,views.bit_length()/8)
                snippet=video.get("snippet",{})
                candidates=list(snippet.get("tags",[]))
                candidates.extend(re.findall(r"[\wÀ-ÿ#]{3,40}",snippet.get("title","")))
                for raw in candidates:
                    term=raw.strip().lstrip("#").strip()
                    key=re.sub(r"[^\wÀ-ÿ]","",term).casefold()
                    if 3<=len(key)<=35: weights[term.casefold()]=weights.get(term.casefold(),0)+score
        terms=sorted(weights,key=weights.get,reverse=True)[:40]
        cache.write_text(json.dumps({"key":cache_key,"updated":time.time(),"terms":terms},ensure_ascii=False),encoding="utf-8")
        return terms
    except Exception as exc:
        logging.warning("Tendências atuais indisponíveis: %s",exc)
        return []


def select_moments(segments, cfg):
    from openai import OpenAI
    client=OpenAI(base_url=os.getenv("OLLAMA_BASE_URL","http://127.0.0.1:11434/v1"),api_key="ollama")
    model=cfg.get("ollama_model","qwen3:8b")
    windows=candidate_windows(segments,cfg)
    if not windows: return []
    max_clips=max(1,min(20,int(cfg.get("max_clips_per_chunk",12))))
    schema = {"type":"object","properties":{"clips":{"type":"array","items":{"type":"object","properties":{
        "window_id":{"type":"string"},"topic":{"type":"string"},"scope":{"type":"string"},
        "confidence":{"type":"number"},"reason":{"type":"string"}},"required":["window_id","topic","scope","confidence","reason"],"additionalProperties":False}}},"required":["clips"],"additionalProperties":False}
    candidate_text="\n".join(f"{w['id']} ({w['start']:.1f}-{w['end']:.1f}s)\nCONTEXTO ANTERIOR: {w['context_before']}\nTRECHO CANDIDATO: {w['text']}\nCONTEXTO POSTERIOR: {w['context_after']}" for w in windows)
    discovery = client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[{"role":"system","content":f"Você analisa uma transcrição para descobrir os assuntos que o próprio vídeo aborda. Não use uma lista externa de assuntos. Agrupe janelas por assunto/escopo e escolha os melhores trechos que representem cada discussão distinta. Cubra a conversa toda, sem repetir o mesmo ponto; escolha outra janela do mesmo assunto apenas para uma ideia distinta. Cada corte precisa ser autossuficiente, ter abertura e conclusão naturais, não começar no meio de uma frase, e não depender do contexto antes/depois. Use o contexto vizinho apenas para julgar se a janela candidata começa ou termina abruptamente; o áudio final contém somente o trecho candidato. Não invente tópicos, fatos ou falas. Use IDs exatos. Selecione no máximo {max_clips} cortes, retorne confiança entre 0 e 1, e omita trechos duvidosos ou sem valor."},
                  {"role":"user","content":"Descubra os assuntos substantivos e o escopo de cada corte neste vídeo. Avalie candidatos em ordem temporal. Para cada corte, retorne o ID exato, um rótulo curto para o assunto, uma frase com o escopo específico, confiança e motivo.\n"+candidate_text}],
        response_format={"type":"json_schema","json_schema":{"name":"topic_discovery","strict":True,"schema":schema}})
    selected=json.loads(discovery.choices[0].message.content)["clips"][:max_clips]
    by_id={w["id"]:w for w in windows}; picked=[]
    for item in selected:
        w=by_id.get(item["window_id"])
        if not w or float(item.get("confidence",0))<0.65: continue
        if any(min(w["end"],x["end"])-max(w["start"],x["start"])>8 for x in picked): continue
        picked.append({**item,"start":w["start"],"end":w["end"],"text":w["text"]})
    if not picked: return []

    trends=current_topic_trends(list(dict.fromkeys(x["topic"] for x in picked)))
    metadata_schema={"type":"object","properties":{"clips":{"type":"array","items":{"type":"object","properties":{
        "window_id":{"type":"string"},"title":{"type":"string"},"description":{"type":"string"},
        "hashtags":{"type":"array","items":{"type":"string"}},"trending_hashtags":{"type":"array","items":{"type":"string"}}},
        "required":["window_id","title","description","hashtags","trending_hashtags"],"additionalProperties":False}}},"required":["clips"],"additionalProperties":False}
    picked_text="\n".join(f"{x['window_id']} | assunto: {x['topic']} | escopo: {x['scope']} | motivo: {x['reason']} | fala: {x['text']}" for x in picked)
    hashtag_strategy=cfg.get("hashtag_strategy","balanced")
    strategy_guidance={
        "balanced":"Estratégia equilibrada: combine 2-3 hashtags específicas do assunto, 1 categoria relacionada e no máximo 1 termo de tendência validado.",
        "search":"Estratégia de busca: priorize palavras e expressões específicas ditas no corte e o nome claro do assunto; não use tendências se não ajudarem a descrever o conteúdo.",
        "trend_aware":"Estratégia de tendência relevante: use até 2 termos da lista atual somente se forem muito pertinentes ao trecho e complete com 2-3 hashtags específicas do assunto.",
    }.get(hashtag_strategy,"")
    metadata=client.chat.completions.create(
        model=model,temperature=0.2,
        messages=[{"role":"system","content":"Escreva metadados fiéis para cada corte. O título deve ter até 70 caracteres, ser específico, claro e fiel ao trecho, sem clickbait enganoso. A descrição resume só o que o clipe realmente diz, sem inventar contexto. Sugira de 3 a 5 hashtags relevantes ao assunto e à fala. Use hashtag de tendência somente quando ela estiver na lista de sinais recentes e corresponder claramente ao clipe; trending_hashtags precisa ser subconjunto da lista e das hashtags sugeridas. Não use hashtags genéricas só por alcance. "+strategy_guidance},
                  {"role":"user","content":f"Sinais de termos em vídeos recentes populares no YouTube Brasil (podem estar vazios): {json.dumps(trends,ensure_ascii=False)}\n\nGere metadados independentes para cada corte. Retorne de volta o window_id exato.\n{picked_text}"}],
        response_format={"type":"json_schema","json_schema":{"name":"clip_metadata","strict":True,"schema":metadata_schema}})
    metadata_by_id={x["window_id"]:x for x in json.loads(metadata.choices[0].message.content)["clips"]}
    valid=[]
    for c in picked:
        w=by_id.get(c["window_id"])
        data=metadata_by_id.get(c["window_id"])
        if not w or not data: continue
        tags=[]
        for tag in data["hashtags"]:
            clean=re.sub(r"[^\wÀ-ÿ]","",tag.strip().lstrip("#"))
            if clean and clean.casefold() not in {x.casefold() for x in tags}: tags.append(clean[:35])
        trend_set={re.sub(r"[^\wÀ-ÿ]","",x).casefold() for x in trends}
        trend_tags=[re.sub(r"[^\wÀ-ÿ]","",tag.strip().lstrip("#")) for tag in data["trending_hashtags"]]
        trend_tags=[tag for tag in trend_tags if tag.casefold() in trend_set and tag.casefold() in {x.casefold() for x in tags}]
        valid.append({**c,"title":data["title"],"description":data["description"],"start":w["start"],"end":w["end"],"hashtags":tags[:5],"trending_hashtags":trend_tags[:3]})
    valid.sort(key=lambda item:item["start"])
    return valid


def make_srt(segments, start, end, dest):
    chosen = [s for s in segments if s["end"] > start and s["start"] < end]
    def ts(sec):
        ms = int(max(0, sec)*1000); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    lines=[]
    for i,s in enumerate(chosen,1):
        a=max(0,s["start"]-start); b=max(a+.2,min(end-start,s["end"]-start))
        lines += [str(i),f"{ts(a)} --> {ts(b)}",s["text"],""]
    dest.write_text("\n".join(lines), encoding="utf-8")


def ffmpeg_binary():
    system_binary=shutil.which("ffmpeg")
    if system_binary: return system_binary
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("Instale as dependências do projeto (incluem FFmpeg) ou instale FFmpeg no PATH.") from exc


def render(source, clip, segments, index, cfg):
    stem=source.stem
    output=Path(cfg.get("output_folder") or OUTPUT).expanduser()
    output.mkdir(parents=True,exist_ok=True)
    base=output/f"{stem}_{index:02d}"
    mp4, srt=base.with_suffix(".mp4"),base.with_suffix(".srt")
    make_srt(segments,clip["start"],clip["end"],srt)
    layout=cfg.get("video_layout","vertical_center")
    if layout not in VIDEO_LAYOUTS: layout="vertical_center"
    style={
        "vertical_center":"FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=150",
        "blurred_fit":"FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=105",
        "caption_focus":"FontName=Arial,FontSize=25,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=165",
    }[layout]
    subtitle_path=srt.as_posix().replace("\\","/").replace(":","\\:").replace("'","\\'")
    subtitles=f"subtitles='{subtitle_path}':force_style='{style}'"
    common=[ffmpeg_binary(),"-y","-ss",str(clip["start"]),"-i",str(source),"-t",str(clip["end"]-clip["start"])]
    if layout=="blurred_fit":
        graph=("[0:v]split=2[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
               "crop=1080:1920,boxblur=24:10[bgblur];[fg]scale=1080:1920:force_original_aspect_ratio=decrease[front];"
               f"[bgblur][front]overlay=(W-w)/2:(H-h)/2,{subtitles}[v]")
        command=common+["-filter_complex",graph,"-map","[v]","-map","0:a?"]
    else:
        vf=f"crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920,{subtitles}"
        command=common+["-vf",vf]
    command += ["-c:v","libx264","-preset","medium","-crf","21","-c:a","aac","-b:a","128k","-movflags","+faststart",str(mp4)]
    subprocess.run(command,check=True,capture_output=True,text=True)
    return mp4


def process_incoming(cfg):
    for source in sorted(p for p in INCOMING.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS):
        if already_processed(source): continue
        set_progress("transcribing", source.name, "Convertendo fala em texto", 5)
        logging.info("Transcrevendo %s", source.name)
        segments=transcribe(source,cfg)
        if not segments: logging.warning("Sem fala detectável em %s",source.name); continue
        chunk_seconds=float(cfg.get("analysis_chunk_seconds",900))
        overlap=float(cfg.get("chunk_overlap_seconds",60))
        clips=[]; chunk_start=float(segments[0]["start"]); accepted=[]
        while chunk_start < float(segments[-1]["end"]):
            chunk_end=chunk_start+chunk_seconds
            subset=[s for s in segments if s["end"]>chunk_start and s["start"]<chunk_end]
            if subset:
                set_progress("analyzing", source.name, "Identificando assuntos e escolhendo trechos", 35)
                for clip in select_moments(subset,cfg):
                    if any(min(clip["end"],old["end"])-max(clip["start"],old["start"])>8 for old in accepted):
                        continue
                    accepted.append(clip)
                    clips.append(clip)
            if chunk_end>=float(segments[-1]["end"]): break
            chunk_start=chunk_end-overlap
        clips.sort(key=lambda item:item["start"])
        if not clips: logging.info("Nenhum trecho adequado em %s",source.name); continue
        for i,c in enumerate(clips,1):
            set_progress("rendering", source.name, f"Preparando corte {i} de {len(clips)}", 60+int(35*(i-1)/max(1,len(clips))))
            out=render(source,c,segments,i,cfg)
            with db_conn() as con:
                con.execute("INSERT OR IGNORE INTO clips(source,start,end,path,topic,scope,layout,title,description,hashtags,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (str(source),c["start"],c["end"],str(out),c["topic"],c["scope"],cfg.get("video_layout","vertical_center"),c["title"],c["description"],json.dumps(c["hashtags"],ensure_ascii=False),"ready",datetime.now(timezone.utc).isoformat()))
        logging.info("Criados %d cortes para %s",len(clips),source.name)
        set_progress("ready", source.name, f"{len(clips)} cortes prontos na fila", 100)


def youtube_client():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    scope=["https://www.googleapis.com/auth/youtube.upload"]
    token=ROOT/"token.json"; secret=ROOT/"youtube_client_secret.json"
    creds=Credentials.from_authorized_user_file(str(token),scope) if token.exists() else None
    if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
    if not creds or not creds.valid:
        if not secret.exists(): raise RuntimeError("Adicione youtube_client_secret.json do seu projeto Google Cloud.")
        creds=InstalledAppFlow.from_client_secrets_file(str(secret),scope).run_local_server(port=0)
        token.write_text(creds.to_json(),encoding="utf-8")
    return build("youtube","v3",credentials=creds),MediaFileUpload


def _social_request(url, *, token=None, body=None, method=None, headers=None, timeout=60, form=False):
    request_headers={"User-Agent":"AuroraGloriosaCortes/1.0"}
    if headers: request_headers.update(headers)
    if token: request_headers["Authorization"]="Bearer "+token
    data=None
    if body is not None:
        data=urllib.parse.urlencode(body).encode("utf-8") if form and isinstance(body,dict) else json.dumps(body).encode("utf-8")
        request_headers["Content-Type"]="application/x-www-form-urlencoded" if form and isinstance(body,dict) else "application/json"
    req=urllib.request.Request(url,data=data,headers=request_headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            result=json.loads(response.read().decode("utf-8"))
            error=result.get("error") if isinstance(result,dict) else None
            if error and error.get("code") not in (None,"ok"):
                raise RuntimeError(f"{error.get('message') or error.get('code')} (ID {error.get('log_id','')})")
            return result
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode("utf-8",errors="replace")[:1200]
        raise RuntimeError(f"API retornou HTTP {exc.code}: {detail}") from exc


def _upload_https(url, file_path, headers):
    import http.client
    from urllib.parse import urlsplit
    target=urlsplit(url)
    conn=http.client.HTTPSConnection(target.hostname,target.port or 443,timeout=1800)
    try:
        conn.putrequest("POST",target.path+("?"+target.query if target.query else ""))
        conn.putheader("Content-Length",str(Path(file_path).stat().st_size))
        for key,value in headers.items(): conn.putheader(key,str(value))
        conn.endheaders()
        with open(file_path,"rb") as source:
            while block:=source.read(4*1024*1024): conn.send(block)
        response=conn.getresponse(); raw=response.read()
        if response.status>=300: raise RuntimeError(f"Falha ao enviar vídeo (HTTP {response.status}): {raw.decode('utf-8',errors='replace')[:1000]}")
        result=json.loads(raw.decode("utf-8")) if raw else {}
        if isinstance(result,dict) and result.get("success") is False: raise RuntimeError(f"Meta recusou o arquivo: {result}")
        return result
    finally: conn.close()


def publish_instagram_reel(path, title, description, hashtags, cfg, secrets):
    user_id=secrets.get("instagram_user_id","").strip()
    token=secrets.get("instagram_access_token","").strip()
    if not user_id or not token: raise RuntimeError("Conecte o Instagram profissional e salve o ID da conta e token da Meta.")
    version=cfg.get("instagram_api_version","v26.0")
    caption=(title+"\n\n"+description+"\n\n"+" ".join("#"+x for x in hashtags)).strip()[:2200]
    base=f"https://graph.facebook.com/{version}"
    container=_social_request(f"{base}/{user_id}/media",token=token,body={"media_type":"REELS","upload_type":"resumable","caption":caption,"share_to_feed":"true"},method="POST",form=True)
    container_id=container.get("id")
    upload_uri=container.get("uri") or (f"https://rupload.facebook.com/ig-api-upload/{version}/{container_id}" if container_id else "")
    if not container_id or not upload_uri: raise RuntimeError(f"Meta não criou o espaço do Reel: {container}")
    _upload_https(upload_uri,path,{"Authorization":"OAuth "+token,"offset":"0","file_size":Path(path).stat().st_size,"Content-Type":"application/octet-stream"})
    status={}
    for _ in range(120):
        time.sleep(5)
        status=_social_request(f"{base}/{container_id}?fields=status_code,status&access_token={urllib.parse.quote(token)}")
        state=status.get("status_code")
        if state=="FINISHED": break
        if state in {"ERROR","EXPIRED"}: raise RuntimeError(f"Instagram não processou o Reel: {status.get('status','')}")
    else: raise RuntimeError("Instagram ainda está processando o Reel; tente novamente mais tarde.")
    published=_social_request(f"{base}/{user_id}/media_publish",token=token,body={"creation_id":container_id},method="POST",form=True)
    return published.get("id") or container_id


def publish_tiktok_video(path, title, description, hashtags, cfg, secrets):
    token=secrets.get("tiktok_access_token","").strip()
    if not token: raise RuntimeError("Conecte o TikTok e salve um token com permissão video.publish.")
    api="https://open.tiktokapis.com/v2"
    creator=_social_request(api+"/post/publish/creator_info/query/",token=token,body={},method="POST")
    creator_data=creator.get("data",{})
    options=creator_data.get("privacy_level_options",[])
    desired={"public":"PUBLIC_TO_EVERYONE","unlisted":"SELF_ONLY","private":"SELF_ONLY"}.get(cfg.get("youtube_privacy","private"),"SELF_ONLY")
    if desired not in options: raise RuntimeError("O TikTok não oferece a visibilidade escolhida. Privacidades permitidas: "+", ".join(options or ["indisponíveis"]))
    privacy=desired
    caption=(title+" "+" ".join("#"+x for x in hashtags)).strip()
    size=Path(path).stat().st_size
    chunk_size=min(10*1024*1024,size)
    total=math.ceil(size/chunk_size)
    init=_social_request(api+"/post/publish/video/init/",token=token,body={
        "post_info":{"title":caption[:2200],"privacy_level":privacy,"disable_duet":False,"disable_comment":False,"disable_stitch":False},
        "source_info":{"source":"FILE_UPLOAD","video_size":size,"chunk_size":chunk_size,"total_chunk_count":total}},method="POST")
    data=init.get("data",{}); publish_id=data.get("publish_id"); upload_url=data.get("upload_url")
    if not publish_id or not upload_url: raise RuntimeError(f"TikTok não iniciou o envio: {init.get('error',init)}")
    with open(path,"rb") as source:
        start=0
        while start<size:
            block=source.read(chunk_size); end=start+len(block)-1
            req=urllib.request.Request(upload_url,data=block,method="PUT",headers={"Content-Type":"video/mp4","Content-Range":f"bytes {start}-{end}/{size}"})
            try:
                with urllib.request.urlopen(req,timeout=600) as response:
                    if response.status not in (200,201,206): raise RuntimeError(f"TikTok upload HTTP {response.status}")
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"Falha ao enviar para TikTok (HTTP {exc.code}): {exc.read().decode('utf-8',errors='replace')[:1000]}") from exc
            start=end+1
    for _ in range(120):
        time.sleep(5)
        result=_social_request(api+"/post/publish/status/fetch/",token=token,body={"publish_id":publish_id},method="POST")
        status=result.get("data",{}).get("status")
        if status=="PUBLISH_COMPLETE": return publish_id
        if status in {"FAILED","PUBLISH_FAILED"}: raise RuntimeError(f"TikTok não publicou: {result.get('data',{}).get('fail_reason','falha sem detalhe')}")
    raise RuntimeError("TikTok ainda está processando o vídeo; consulte a caixa de entrada do perfil.")


def publish_one(platform,row,cfg,secrets,multi):
    clip_id,path,topic,scope,title,description,raw_tags=row[:7]
    tags=json.loads(raw_tags or "[]")
    if platform=="instagram": return publish_instagram_reel(path,title,description,tags,cfg,secrets),"published",None
    if platform=="tiktok": return publish_tiktok_video(path,title,description,tags,cfg,secrets),"published",None
    if platform!="youtube": raise RuntimeError("Rede social não reconhecida.")
    service,MediaFileUpload=youtube_client()
    now=datetime.now(timezone.utc)
    publish_at=None
    if cfg.get("posting_mode","scheduled")=="scheduled" and not multi:
        with db_conn() as con:
            previous=con.execute("SELECT MAX(COALESCE(scheduled_at,published_at)) FROM clips WHERE status IN ('published','scheduled','uploaded')").fetchone()[0]
        last_time=datetime.fromisoformat(previous) if previous else None
        publish_at=max(now+timedelta(minutes=10),last_time+timedelta(hours=float(cfg.get("posting_interval_hours",6))) if last_time else now+timedelta(minutes=10))
        status_body={"privacyStatus":"private","publishAt":publish_at.isoformat(timespec="seconds").replace("+00:00","Z")}
        saved_status="scheduled"
    else:
        privacy=cfg.get("youtube_privacy","private")
        status_body={"privacyStatus":privacy}
        saved_status="published" if privacy=="public" else "uploaded"
    topic_scope=f"Assunto: {topic}\n{scope}\n\n" if topic or scope else ""
    final_description=topic_scope+description+("\n\n"+" ".join("#"+tag for tag in tags) if tags else "")
    request=service.videos().insert(part="snippet,status",body={"snippet":{"title":title[:100],"description":final_description[:5000],"tags":tags,"categoryId":str(cfg.get("category_id","22"))},"status":{**status_body,"selfDeclaredMadeForKids":bool(cfg.get("made_for_kids",False))}},media_body=MediaFileUpload(path,chunksize=-1,resumable=True))
    response=None
    while response is None: _,response=request.next_chunk()
    return response["id"],saved_status,publish_at.isoformat() if publish_at else None


def publish_next(cfg):
    allowed={"youtube","instagram","tiktok"}
    platforms=[p for p in cfg.get("publish_platforms",["youtube"]) if p in allowed]
    if not platforms: return
    if len(platforms)>1 and cfg.get("posting_mode")=="scheduled":
        logging.warning("Publicação conjunta pausada: selecione o modo imediato para redes múltiplas.")
        return
    now=datetime.now(timezone.utc)
    interval=max(0.25,float(cfg.get("posting_interval_hours",6)))
    with db_conn() as con:
        con.execute("BEGIN IMMEDIATE")
        for platform in platforms:
            con.execute("""INSERT OR IGNORE INTO platform_posts(clip_id,platform,status,platform_id,published_at,approved)
                SELECT id,?,CASE WHEN ?='youtube' AND youtube_id IS NOT NULL THEN
                    CASE WHEN status='scheduled' THEN 'scheduled' ELSE 'published' END ELSE 'ready' END,
                    CASE WHEN ?='youtube' THEN youtube_id ELSE NULL END,
                    CASE WHEN ?='youtube' THEN COALESCE(scheduled_at,published_at) ELSE NULL END,
                    CASE WHEN ? THEN 1 ELSE 0 END FROM clips
                    WHERE status IN ('ready','uploading') OR (?='youtube' AND youtube_id IS NOT NULL)""",
                (platform,platform,platform,platform,int(bool(cfg.get("auto_publish",False))),platform))
        row=con.execute("""SELECT c.id,c.path,c.topic,c.scope,c.title,c.description,c.hashtags
            FROM clips c JOIN platform_posts p ON p.clip_id=c.id
            WHERE p.platform IN (%s) AND p.status IN ('ready','failed') AND (p.approved=1 OR ?=1)
              AND (p.next_attempt_at IS NULL OR p.next_attempt_at<=?)
            ORDER BY c.id LIMIT 1""" % ",".join("?" for _ in platforms),(*platforms,int(bool(cfg.get("auto_publish",False))),now.isoformat())).fetchone()
        if not row: con.commit(); return
        post_rows=con.execute("SELECT platform,status,next_attempt_at FROM platform_posts WHERE clip_id=? AND platform IN (%s)" % ",".join("?" for _ in platforms),(row[0],*platforms)).fetchall()
        # Publish all selected destinations for a clip in the same cycle. A retry only touches destinations that have not succeeded.
        pending=[p for p,status,retry in post_rows if status in ("ready","failed") and (not retry or datetime.fromisoformat(retry)<=now)]
        if not pending: con.commit(); return
        timestamps=[datetime.fromisoformat(v) for (v,) in con.execute("SELECT published_at FROM platform_posts WHERE published_at IS NOT NULL").fetchall()]
        last_time=max(timestamps) if timestamps else None
        if last_time and now<last_time+timedelta(hours=interval): con.commit(); return
        for platform in pending:
            con.execute("UPDATE platform_posts SET status='uploading',attempts=attempts+1,last_error=NULL WHERE clip_id=? AND platform=?",(row[0],platform))
        con.execute("UPDATE clips SET status='uploading',attempts=attempts+1,last_error=NULL WHERE id=?",(row[0],))
        con.commit()
    set_progress("publishing", Path(row[1]).name, "Publicando os cortes aprovados", 100)
    secrets=social_secrets()
    multi=len(platforms)>1
    from concurrent.futures import ThreadPoolExecutor,as_completed
    results=[]
    with ThreadPoolExecutor(max_workers=len(pending)) as pool:
        futures={pool.submit(publish_one,platform,row,cfg,secrets,multi):platform for platform in pending}
        for future in as_completed(futures):
            platform=futures[future]
            try: results.append((platform,*future.result(),None))
            except Exception as exc: results.append((platform,None,None,None,exc))
    with db_conn() as con:
        for platform,post_id,saved_status,publish_at,error in results:
            if error:
                attempts=con.execute("SELECT attempts FROM platform_posts WHERE clip_id=? AND platform=?",(row[0],platform)).fetchone()[0]
                delay=min(21600,60*(2**min(max(0,attempts-1),8)))
                retry=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()
                con.execute("UPDATE platform_posts SET status='failed',next_attempt_at=?,last_error=? WHERE clip_id=? AND platform=?",(retry,str(error)[:1000],row[0],platform))
                logging.error("Falha ao publicar %s para %s: %s",row[4],platform,error)
                continue
            timestamp=datetime.now(timezone.utc).isoformat()
            post_status="scheduled" if saved_status=="scheduled" else "published"
            con.execute("UPDATE platform_posts SET status=?,platform_id=?,published_at=?,next_attempt_at=NULL,last_error=NULL WHERE clip_id=? AND platform=?",(post_status,post_id,publish_at or timestamp,row[0],platform))
            if platform=="youtube":
                con.execute("UPDATE clips SET youtube_id=?,published_at=?,scheduled_at=? WHERE id=?",(post_id,timestamp,publish_at,row[0]))
            logging.info("Publicado %s em %s (%s)",row[4],platform,post_id)
        remaining=con.execute("SELECT COUNT(*) FROM platform_posts WHERE clip_id=? AND platform IN (%s) AND status NOT IN ('published','scheduled')" % ",".join("?" for _ in platforms),(row[0],*platforms)).fetchone()[0]
        if remaining:
            errors=con.execute("SELECT platform,last_error FROM platform_posts WHERE clip_id=? AND platform IN (%s) AND status='failed'" % ",".join("?" for _ in platforms),(row[0],*platforms)).fetchall()
            summary="; ".join(f"{p}: {e}" for p,e in errors)
            con.execute("UPDATE clips SET status='ready',next_attempt_at=NULL,last_error=? WHERE id=?",(summary[:1000],row[0]))
        else:
            con.execute("UPDATE clips SET status='scheduled',next_attempt_at=NULL,last_error=NULL WHERE id=? AND ?='scheduled'",(row[0],"scheduled" if platforms==["youtube"] and cfg.get("posting_mode")=="scheduled" else "published"))
            con.execute("UPDATE clips SET status='published' WHERE id=? AND status!='scheduled'",(row[0],))


def cycle_once():
    cfg=config()
    for p in (INCOMING,OUTPUT,STATE): p.mkdir(parents=True,exist_ok=True)
    if cfg.get("pipeline_paused",False):
        set_progress("paused", "", "Fila pausada. Retome quando quiser.", 0)
        return
    process_incoming(cfg)
    publish_next(cfg)
    set_progress("idle", "", "Tudo atualizado. Aguardando novos vídeos ou o próximo horário.", 0)


def main():
    for p in (INCOMING,OUTPUT,STATE): p.mkdir(parents=True,exist_ok=True)
    logging.info("Robô iniciado")
    while True:
        try:
            cycle_once()
        except Exception:
            logging.exception("Falha neste ciclo; será tentada novamente")
        time.sleep(60)

if __name__ == "__main__": main()
