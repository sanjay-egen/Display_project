#!/usr/bin/env python3
import io, json, os, sys, time
from pathlib import Path
import pygame
import requests

SOURCE=os.getenv('SOURCE','drive').lower()
LAPTOP_IP=os.getenv('LAPTOP_IP','192.168.1.7')
SERVER_URL=f"http://{LAPTOP_IP}:{os.getenv('SERVER_PORT','8000')}"
DRIVE_FOLDER_ID=os.getenv('DRIVE_FOLDER_ID','').split('/folders/')[-1].split('?')[0].strip('/')
DRIVE_CREDENTIALS=os.getenv('DRIVE_CREDENTIALS','/home/pi/image_receiver/credentials.json')
DRIVE_TOKEN=os.getenv('DRIVE_TOKEN','/home/pi/image_receiver/token.json')
IMAGE_FOLDER=Path(os.getenv('IMAGE_FOLDER','/home/pi/image_receiver/images'))
SLIDE_DELAY=float(os.getenv('SLIDE_DELAY','5'))
UPDATE_CHECK_INTERVAL=float(os.getenv('UPDATE_CHECK_INTERVAL','30'))
EXTS=('.jpg','.jpeg','.png','.webp')
SCOPES=['https://www.googleapis.com/auth/drive.readonly']
MANIFEST=IMAGE_FOLDER/'.image_manifest.json'

def log(*x): print(*x,flush=True)
def safe(n): return os.path.basename(n).replace('/','_').replace('\\','_') or 'unnamed'
def md5(p):
    if not p.exists(): return None
    import hashlib
    h=hashlib.md5()
    with p.open('rb') as f:
        for c in iter(lambda:f.read(1024*1024),b''): h.update(c)
    return h.hexdigest()
def load_manifest():
    try: return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    except Exception: return {}
def save_manifest(m):
    t=MANIFEST.with_suffix('.tmp'); t.write_text(json.dumps(m,indent=2)); os.replace(t,MANIFEST)

def laptop_items():
    r=requests.get(SERVER_URL+'/images',timeout=5,headers={'Cache-Control':'no-cache'}); r.raise_for_status()
    data=r.json(); out={}
    for x in data:
        if isinstance(x,str): n=safe(x); meta={'name':n}
        else: n=safe(str(x.get('name',''))); meta=dict(x); meta['name']=n
        if n.lower().endswith(EXTS): out[n]=meta
    return out

def download_url(name):
    p=IMAGE_FOLDER/name; t=p.with_suffix(p.suffix+'.tmp'); p.parent.mkdir(parents=True,exist_ok=True)
    try:
        with requests.get(SERVER_URL+'/'+name,timeout=30,stream=True,headers={'Cache-Control':'no-cache'}) as r:
            r.raise_for_status()
            with t.open('wb') as f:
                for c in r.iter_content(1024*1024):
                    if c: f.write(c)
        os.replace(t,p); return True
    except Exception as e:
        log('Laptop download error:',name,e); t.unlink(missing_ok=True); return False

def drive_auth():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    creds=None
    if os.path.exists(DRIVE_TOKEN): creds=Credentials.from_authorized_user_file(DRIVE_TOKEN,SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
        else:
            if not os.path.exists(DRIVE_CREDENTIALS): raise RuntimeError('Missing '+DRIVE_CREDENTIALS)
            creds=InstalledAppFlow.from_client_secrets_file(DRIVE_CREDENTIALS,SCOPES).run_local_server(port=0)
        Path(DRIVE_TOKEN).parent.mkdir(parents=True,exist_ok=True); Path(DRIVE_TOKEN).write_text(creds.to_json())
    return build('drive','v3',credentials=creds,cache_discovery=False)

def drive_items(service):
    if not DRIVE_FOLDER_ID: raise RuntimeError('Set DRIVE_FOLDER_ID in slideshow.env')
    out={}
    def walk(fid,rel=''):
        token=None
        while True:
            r=service.files().list(q=f"'{fid}' in parents and trashed=false",spaces='drive',pageSize=1000,pageToken=token,fields='nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)').execute()
            for x in r.get('files',[]):
                n=safe(x['name'])
                if x['mimeType']=='application/vnd.google-apps.folder': walk(x['id'],os.path.join(rel,n))
                elif n.lower().endswith(EXTS): out[os.path.join(rel,n)] = x
            token=r.get('nextPageToken')
            if not token: break
    walk(DRIVE_FOLDER_ID); return out

def download_drive(service,name,meta):
    from googleapiclient.http import MediaIoBaseDownload
    p=IMAGE_FOLDER/name; t=p.with_suffix(p.suffix+'.tmp'); p.parent.mkdir(parents=True,exist_ok=True)
    try:
        req=service.files().get_media(fileId=meta['id'])
        with t.open('wb') as f:
            d=MediaIoBaseDownload(f,req,chunksize=10*1024*1024); done=False
            while not done:
                status,done=d.next_chunk()
                if status: log(f"{name}: {status.progress()*100:.1f}%")
        os.replace(t,p); return True
    except Exception as e:
        log('Drive download error:',name,e); t.unlink(missing_ok=True); return False

def sync_drive(service,manifest):
    remote=drive_items(service); changed=False
    for name,x in remote.items():
        key=[x['id'],x.get('md5Checksum'),x.get('size'),x.get('modifiedTime')]
        if (IMAGE_FOLDER/name).exists() and manifest.get(name,{}).get('key')==key: continue
        log('New/changed:',name)
        if download_drive(service,name,x): manifest[name]={'key':key,'md5':md5(IMAGE_FOLDER/name)}; changed=True
    for name in list(manifest):
        if name not in remote:
            p=IMAGE_FOLDER/name; p.unlink(missing_ok=True); del manifest[name]; changed=True; log('Removed:',name)
    if changed: save_manifest(manifest)
    return sorted(remote,key=str.lower)

def sync_laptop(manifest):
    remote=laptop_items(); names=sorted(remote,key=str.lower); changed=False
    for name,x in remote.items():
        key=x.get('etag') or x.get('md5') or (x.get('size'),x.get('mtime'),x.get('modifiedTime'))
        if (IMAGE_FOLDER/name).exists() and key is not None and manifest.get(name,{}).get('key')==key: continue
        if (IMAGE_FOLDER/name).exists() and key is None and name in manifest: continue
        log('Downloading:',name)
        if download_url(name): manifest[name]={'key':key,'md5':md5(IMAGE_FOLDER/name)}; changed=True
    for name in list(manifest):
        if name not in remote: (IMAGE_FOLDER/name).unlink(missing_ok=True); del manifest[name]; changed=True; log('Removed:',name)
    if changed: save_manifest(manifest)
    return names

IMAGE_FOLDER.mkdir(parents=True,exist_ok=True); manifest=load_manifest(); service=None
if SOURCE=='drive':
    try: service=drive_auth()
    except Exception as e: log('Drive authentication unavailable:',e)

def sync():
    global image_list
    try:
        image_list=sync_drive(service,manifest) if SOURCE=='drive' and service else sync_laptop(manifest)
    except Exception as e:
        log('Sync failed; keeping cached images:',e)
        image_list=sorted([str(p.relative_to(IMAGE_FOLDER)) for p in IMAGE_FOLDER.rglob('*') if p.is_file() and p.suffix.lower() in EXTS],key=str.lower)

pygame.init(); screen=pygame.display.set_mode((0,0),pygame.FULLSCREEN); pygame.mouse.set_visible(False)
W,H=screen.get_size(); log('Slideshow:',SOURCE,W,H); sync()
idx=0; last_slide=time.time(); last_sync=0

def show(name):
    p=IMAGE_FOLDER/name
    if not p.exists(): return
    try:
        im=pygame.image.load(str(p)).convert(); iw,ih=im.get_size(); s=min(W/iw,H/ih); im=pygame.transform.smoothscale(im,(max(1,int(iw*s)),max(1,int(ih*s))))
        screen.fill((0,0,0)); screen.blit(im,((W-im.get_width())//2,(H-im.get_height())//2)); pygame.display.flip(); log('Showing:',name)
    except Exception as e: log('Display error:',name,e)
if image_list: show(image_list[0])
while True:
    try:
        now=time.time()
        for e in pygame.event.get():
            if e.type==pygame.QUIT or (e.type==pygame.KEYDOWN and e.key==pygame.K_ESCAPE): raise KeyboardInterrupt
        if now-last_sync>=UPDATE_CHECK_INTERVAL:
            old=image_list[:]; sync(); last_sync=now
            if image_list and image_list!=old: idx=min(idx,len(image_list)-1); show(image_list[idx]); last_slide=now
        if image_list and now-last_slide>=SLIDE_DELAY:
            idx=(idx+1)%len(image_list); show(image_list[idx]); last_slide=now
        time.sleep(.05)
    except KeyboardInterrupt: pygame.quit(); sys.exit(0)
    except Exception as e: log('Loop error:',e); time.sleep(2)
