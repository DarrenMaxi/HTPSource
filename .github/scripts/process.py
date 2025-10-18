import os
import sys
import json
import re
import hashlib
import zipfile
import shutil
from datetime import datetime, timezone
from pathlib import Path  # <<< ADDED
from bs4 import BeautifulSoup
import requests

def parse_issue_body(body):
    """解析 Issue body，提取表单数据"""
    data = {}
    key_map = {
        "补丁名称": "patchName", "作者/团队名称": "patchAuthor",
        "补丁版本号": "patchVersion", "补丁描述": "description",
        "更新日志 (Changelog)": "changelog", "支持的整合包列表": "supportedModpacks",
        "上传补丁压缩包": "attachment"
    }
    sections = re.split(r'###\s+', body)
    for section in sections:
        if not section.strip():
            continue
        lines = section.split('\n', 1)
        key = lines[0].strip()
        value = lines[1].strip() if len(lines) > 1 else ''
        if key in key_map:
            data[key_map[key]] = value
    return data

def get_attachment_url(html_body):
    """从渲染后的 Issue body (Markdown/HTML) 中解析附件 URL"""
    match = re.search(r'\[.*?\.zip\]\((.*?)\)', html_body)
    if match:
        return match.group(1)
    soup = BeautifulSoup(html_body, 'lxml')
    for a_tag in soup.find_all('a', href=True):
        if a_tag['href'].endswith('.zip'):
            if 'github.com' in a_tag['href']:
                return "https://github.com" + a_tag['href']
    return None

def calculate_sha1(filepath):
    """计算文件的 SHA1 哈希值"""
    sha1 = hashlib.sha1()
    with open(filepath, 'rb') as f:
        while True:
            data = f.read(65536)
            if not data:
                break
            sha1.update(data)
    return sha1.hexdigest()

def create_slug(text):
    """将文本转换为小写、连字符分隔的 slug"""
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[\s_]+', '-', text).strip().lower()

def main():
    issue_body = os.environ.get('ISSUE_BODY', '')
    repo_full_name = os.environ.get('REPO_FULL_NAME', 'user/repo')
    
    data = parse_issue_body(issue_body)
    if not all(k in data for k in ['patchName', 'patchAuthor', 'patchVersion', 'supportedModpacks', 'attachment']):
        print("::error::Issue form is incomplete.")
        sys.exit(1)

    supported_modpacks_raw = data.get('supportedModpacks', '')
    supported_modpacks_list = []
    for line in supported_modpacks_raw.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(',')]
        if len(parts) == 3:
            supported_modpacks_list.append({"type": parts[0], "name": parts[1], "version": parts[2]})
        else:
            print(f"::warning::Skipping malformed line in supported modpacks: {line}")
    
    if not supported_modpacks_list:
        print("::error::No valid supported modpack entries found.")
        sys.exit(1)

    attachment_url = get_attachment_url(data['attachment'])
    if not attachment_url:
        print(f"::error::Could not find attachment URL in body: {data['attachment']}")
        sys.exit(1)
    
    print(f"Downloading attachment from: {attachment_url}")
    response = requests.get(attachment_url, allow_redirects=True)
    if response.status_code != 200:
        print(f"::error::Failed to download attachment. Status: {response.status_code}")
        sys.exit(1)
    
    with open('patch.zip', 'wb') as f:
        f.write(response.content)

    temp_dir = Path("temp_patch") # <<< CHANGED
    if temp_dir.exists(): shutil.rmtree(temp_dir)
    temp_dir.mkdir()
    
    try:
        with zipfile.ZipFile('patch.zip', 'r') as zip_ref:
            if not any(name.startswith('overrides/') for name in zip_ref.namelist()):
                print("::error::Uploaded ZIP does not contain an 'overrides/' folder at its root.")
                sys.exit(1)
            zip_ref.extractall(temp_dir)
    except zipfile.BadZipFile:
        print("::error::Downloaded file is not a valid ZIP file.")
        sys.exit(1)

    manifest = {
        "formatVersion": 1, "patchName": data['patchName'], "patchVersion": data['patchVersion'],
        "patchAuthor": data['patchAuthor'], "description": data['description'], "translationType": "manual",
        "supportedModpacks": supported_modpacks_list, "fileManifest": []
    }

    overrides_path = temp_dir / 'overrides' # <<< CHANGED
    for file_path in overrides_path.rglob('*'):
        if file_path.is_file():
            relative_path = file_path.relative_to(overrides_path).as_posix() # <<< CHANGED
            manifest['fileManifest'].append({
                "operation": "overwrite", "path": relative_path, "targetPath": relative_path,
                "patchedSha1": calculate_sha1(file_path)
            })
    
    author_slug = create_slug(data['patchAuthor'])
    patch_slug = create_slug(data['patchName'])
    patch_dir = Path('patches') / author_slug / patch_slug # <<< CHANGED
    patch_dir.mkdir(parents=True, exist_ok=True)

    # <<< FIXED LINE BELOW
    manifest['updateInfoUrl'] = f"https://raw.githubusercontent.com/{repo_full_name}/main/{patch_dir.as_posix()}/info.json"
    
    manifest_path = temp_dir / 'translation-manifest.json' # <<< CHANGED
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=4, ensure_ascii=False)

    htp_filename = f"{patch_slug}-{data['patchVersion'].lstrip('v')}.htp"
    htp_filepath = patch_dir / htp_filename # <<< CHANGED
    
    with zipfile.ZipFile(htp_filepath, 'w', zipfile.ZIP_DEFLATED) as htp_zip:
        htp_zip.write(manifest_path, 'translation-manifest.json')
        for root, _, files in os.walk(overrides_path):
            for file in files:
                full_path = Path(root) / file
                arcname = full_path.relative_to(temp_dir).as_posix()
                htp_zip.write(full_path, arcname)
    print(f"Generated HTP file: {htp_filepath}")

    info_path = patch_dir / 'info.json' # <<< CHANGED
    new_version_entry = {
        "patchVersion": data['patchVersion'], "releaseDate": datetime.now(timezone.utc).isoformat(),
        "changelog": data['changelog'], "supportedModpackVersions": manifest['supportedModpacks'],
        "downloads": [{
            "type": "direct", "name": "GitHub Raw",
            # <<< FIXED LINE BELOW
            "url": f"https://raw.githubusercontent.com/{repo_full_name}/main/{htp_filepath.as_posix()}",
            "sha1": calculate_sha1(htp_filepath)
        }]
    }
    if info_path.exists():
        with open(info_path, 'r', encoding='utf-8') as f: info_data = json.load(f)
        info_data['versions'].insert(0, new_version_entry)
    else:
        info_data = {
            "formatVersion": 1, "patchId": f"{author_slug}/{patch_slug}", "patchName": data['patchName'],
            "author": data['patchAuthor'], "description": data['description'], "versions": [new_version_entry]
        }
    with open(info_path, 'w', encoding='utf-8') as f:
        json.dump(info_data, f, indent=4, ensure_ascii=False)
    print(f"Updated info.json: {info_path}")

    index_path = Path('index.json') # <<< CHANGED
    if index_path.exists():
        with open(index_path, 'r', encoding='utf-8') as f: index_data = json.load(f)
    else:
        index_data = {"formatVersion": 1, "lastUpdated": "", "patches": {}}
    
    index_data['lastUpdated'] = datetime.now(timezone.utc).isoformat()
    patch_id = f"{author_slug}/{patch_slug}"
    
    for modpack in supported_modpacks_list:
        modpack_key = f"{modpack['type'].lower()}:{modpack['name']}"
        if modpack_key not in index_data['patches']:
            index_data['patches'][modpack_key] = []
        
        summary_exists = any(s['patchId'] == patch_id for s in index_data['patches'][modpack_key])
        if not summary_exists:
            index_data['patches'][modpack_key].append({
                # <<< FIXED LINE BELOW
                "infoPath": f"./{patch_dir.as_posix()}/info.json",
                "patchId": patch_id, "patchName": data['patchName'], "author": data['patchAuthor'],
                "description": data['description'], "latestVersion": data['patchVersion'],
                "translationType": "manual", "availableDownloadTypes": ["direct"]
            })
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, indent=2, ensure_ascii=False)
    print("Updated index.json")
    
    shutil.rmtree(temp_dir)
    os.remove('patch.zip')
    
    branch_name = f"patch/{author_slug}/{patch_slug}-{data['patchVersion'].lstrip('v')}"
    pr_title = f"feat: Add {data['patchName']} {data['patchVersion']}"
    pr_body = f"Adds new patch '{data['patchName']}' version {data['patchVersion']} submitted by @{os.environ.get('ISSUE_AUTHOR')}.\n\nCloses #{os.environ.get('ISSUE_NUMBER')}"
    
    with open(os.environ['GITHUB_OUTPUT'], 'a') as gh_output:
        print(f"branch_name={branch_name}", file=gh_output)
        print(f"pr_title={pr_title}", file=gh_output)
        print(f"pr_body={pr_body}", file=gh_output)

if __name__ == "__main__":
    main()