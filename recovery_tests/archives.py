"""Archives Moodle verifiees puis extraites dans une copie privee neuve."""
from datetime import datetime, timezone
import hashlib
import posixpath
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile

from backup_status.catalog_collector import parse_time
from pilotage_backup.core import BackupError, json_value, parse_utc, sha_file, read_file

ARCHIVES=('moodle.pgdump','moodledata.tar','code.tar','configuration.tar','infrastructure.tar','manifest.txt')
MAX_ARCHIVE=512*1024*1024
MAX_EXPANDED=2*1024*1024*1024


def snapshot_source(config,job,commands):
    wrapper='/usr/local/sbin/'+config['moodle_project']+'-restic'
    rows=json_value(commands.run([wrapper,'--no-cache','--no-lock','snapshots','--json',job['snapshot_id']]))
    if not isinstance(rows,list) or len(rows)!=1:raise BackupError('selected_snapshot_not_unique')
    row=rows[0];point=job['point']
    if (row.get('id')!=job['snapshot_id'] or parse_time(row.get('time'))!=point['archived_at']
            or row.get('hostname')!=point['hostname']):raise BackupError('selected_snapshot_proof_changed')
    tag=config['moodle_project']+'-full' if job['scope']=='moodle' else 'moodle-pilotage-database'
    staging=config['moodle_base']+'/backups/staging' if job['scope']=='moodle' else '/var/lib/moodle-pilotage-backup/staging'
    paths=row.get('paths')
    if (tag not in row.get('tags',[]) or not isinstance(paths,list) or len(paths)!=1
            or not isinstance(paths[0],str) or not paths[0].startswith(staging+'/run-')
            or not re.fullmatch('/[a-zA-Z0-9_./-]+',paths[0]) or '..' in paths[0].split('/')):
        raise BackupError('selected_snapshot_scope_invalid')
    return wrapper,paths[0]


def fetch_moodle(config,job,directory,commands):
    wrapper,source=snapshot_source(config,job,commands)
    proof=commands.run([wrapper,'--no-cache','--no-lock','dump',job['snapshot_id'],source+'/SHA256SUMS'],max_bytes=4096)
    checks={}
    try:
        for line in proof.decode('ascii').splitlines():
            match=re.fullmatch('([0-9a-f]{64})  ([a-zA-Z0-9_.]+)',line)
            if match is None or match[2] in checks:raise ValueError('checksums')
            checks[match[2]]=match[1]
    except (UnicodeError,ValueError):raise BackupError('backup_checksum_manifest_invalid') from None
    if set(checks)!=set(ARCHIVES):raise BackupError('backup_checksum_manifest_invalid')
    for name in ARCHIVES:
        target=directory/name
        if shutil.disk_usage(directory).free<config['min_free_mb']*1024*1024+MAX_ARCHIVE:
            raise BackupError('insufficient_test_space')
        with open(target,'xb') as output:
            commands.run([wrapper,'--no-cache','--no-lock','dump',job['snapshot_id'],source+'/'+name],output=output,max_bytes=MAX_ARCHIVE)
        if sha_file(target)!=checks[name]:raise BackupError('backup_checksum_mismatch')
    text=read_file(directory/'manifest.txt',65536).decode()
    if len(text)>65536 or 'Format sauvegarde : moodle-pfe-v1\n' not in text:
        raise BackupError('backup_manifest_invalid')
    fields={}
    for label in ('Instant sans ecritures UTC','Projet Compose','Base','Moodle','URL source'):
        matches=re.findall('^'+re.escape(label)+' : (.+)$',text,re.M)
        if len(matches)!=1:raise BackupError('backup_manifest_invalid')
        fields[label]=matches[0]
    if fields['Projet Compose']!=config['moodle_project'] or fields['Base']!='moodle':
        raise BackupError('backup_manifest_scope_invalid')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',fields['Moodle']):raise BackupError('backup_version_invalid')
    if parse_utc(fields['Instant sans ecritures UTC'])>parse_utc(job['point']['archived_at']):
        raise BackupError('backup_manifest_clock_invalid')
    if not re.fullmatch('https://[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]{1,5})?',fields['URL source']):
        raise BackupError('backup_url_invalid')
    with open(directory/'moodle.pgdump','rb') as stream:
        if stream.read(5)!=b'PGDMP':raise BackupError('backup_database_format_invalid')
    return fields


def safe_extract(archive,destination,*,selected=None,max_bytes=MAX_EXPANDED,max_members=200000):
    destination=Path(destination)
    if destination.exists():raise BackupError('test_extraction_destination_exists')
    destination.mkdir(mode=0o755)
    links=[];seen=set();expanded=0;found=set()
    with tarfile.open(archive,'r:') as stream:
        for index,member in enumerate(stream):
            if index>=max_members:raise BackupError('backup_archive_too_many_files')
            path=PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.name or any(ord(c)<32 for c in member.name)):
                raise BackupError('backup_archive_unsafe_path')
            name=str(path)
            if name=='.':
                if not member.isdir():raise BackupError('backup_archive_unsafe_path')
                continue
            if name in seen:raise BackupError('backup_archive_duplicate_path')
            seen.add(name)
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise BackupError('backup_archive_unsafe_member')
            expanded+=member.size
            if expanded>max_bytes:raise BackupError('backup_archive_expansion_limit')
            if selected is not None and name not in selected:continue
            if selected is not None and not member.isfile():raise BackupError('backup_configuration_unsafe_member')
            target=destination/name
            if any(p.is_symlink() for p in (target,*target.parents)):raise BackupError('backup_archive_unsafe_path')
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
            if member.isdir():target.mkdir(exist_ok=True,mode=0o755)
            elif member.isfile():
                with stream.extractfile(member) as source,open(target,'xb') as output:
                    shutil.copyfileobj(source,output,1024*1024)
                if target.stat().st_size!=member.size:raise BackupError('backup_archive_truncated')
                target.chmod(0o644);found.add(name)
            else:
                # Liens Composer internes seulement ; aucun lien vers les volumes actifs.
                linked=posixpath.normpath(posixpath.join(str(path.parent),member.linkname)) if member.issym() else posixpath.normpath(member.linkname)
                if linked.startswith('/') or linked=='..' or linked.startswith('../') or '\\' in linked:
                    raise BackupError('backup_archive_unsafe_link')
                links.append((target,linked,member.issym()))
    for target,linked,symbolic in links:
        source=destination/linked
        if not source.is_file() or not source.resolve().is_relative_to(destination.resolve()):
            raise BackupError('backup_archive_unsafe_link')
        if symbolic:target.symlink_to(os_relative(source,target.parent))
        else:
            shutil.copyfile(source,target);target.chmod(0o644)
    if selected is not None and found!=set(selected):raise BackupError('backup_configuration_incomplete')
    return len(found)


def os_relative(source,parent):
    import os
    return os.path.relpath(source,parent)
