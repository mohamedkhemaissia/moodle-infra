"""Reconstituer Moodle sur des donnees privees et un reseau Docker interne."""
import json
import os
from pathlib import Path
import re
import tempfile
import time

import yaml

from pilotage_backup.core import BackupError, json_value, read_file
from .archives import fetch_moodle, safe_extract
from .runtime import DOCKER, LABEL

HOST='moodle-restore.test'
URL='https://'+HOST+':8443'
HTTPD='''ServerRoot "/usr/local/apache2"
ServerName moodle-restore.test
Listen 8443
LoadModule mpm_event_module modules/mod_mpm_event.so
LoadModule unixd_module modules/mod_unixd.so
LoadModule authz_core_module modules/mod_authz_core.so
LoadModule mime_module modules/mod_mime.so
LoadModule dir_module modules/mod_dir.so
LoadModule proxy_module modules/mod_proxy.so
LoadModule proxy_fcgi_module modules/mod_proxy_fcgi.so
LoadModule socache_shmcb_module modules/mod_socache_shmcb.so
LoadModule ssl_module modules/mod_ssl.so
User www-data
Group www-data
ServerTokens Prod
ServerSignature Off
TraceEnable Off
ProxyRequests Off
TypesConfig conf/mime.types
DirectoryIndex index.php
ErrorLog /proc/self/fd/2
<Directory />
  AllowOverride None
  Require all denied
</Directory>
SSLProtocol -all +TLSv1.2 +TLSv1.3
<VirtualHost *:8443>
  ServerName moodle-restore.test
  DocumentRoot "/var/www/moodle/public"
  SSLEngine on
  SSLCertificateFile /usr/local/apache2/conf/tls/server.crt
  SSLCertificateKeyFile /usr/local/apache2/conf/tls/server.key
  <Directory "/var/www/moodle/public">
    Options -Indexes +FollowSymLinks
    AllowOverride None
    Require all granted
    AcceptPathInfo On
    FallbackResource /r.php
  </Directory>
  <FilesMatch "\\.php$">
    SetHandler "proxy:fcgi://php:9000"
  </FilesMatch>
</VirtualHost>
'''
POOL='''[www]
user=www-data
group=www-data
listen=9000
pm=static
pm.max_children=2
clear_env=yes
catch_workers_output=yes
php_admin_value[memory_limit]=256M
'''
LOGIN_CHECK='''$ctx=stream_context_create(['http'=>['timeout'=>15,'follow_location'=>0],
 'ssl'=>['verify_peer'=>true,'verify_peer_name'=>true,'cafile'=>'/test-ca.crt']]);
$html=@file_get_contents('https://moodle-restore.test:8443/login/index.php',false,$ctx,0,2097153);
if ($html===false || strlen($html)>2097152 || !preg_match('/^HTTP\\/\\S+ 200(?: |$)/',$http_response_header[0]??'')
 || !preg_match('/name=["\\\']username["\\\']/i',$html) || !preg_match('/name=["\\\']password["\\\']/i',$html)) {exit(1);}
echo "login_verified\\n";'''


def compose_config(directory,identifier,images):
    """Configuration reconstruite : aucun service, volume ou port archive n'est execute."""
    directory=Path(directory)
    def bind(source,target,readonly=True):
        return {'type':'bind','source':str(directory/source),'target':target,'read_only':readonly,
                'bind':{'create_host_path':False}}
    common={'restart':'no','read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
            'pids_limit':128,'cpus':'0.50','labels':{LABEL:identifier},'networks':['isolated']}
    php={**common,'image':images['php'],'user':'33:33','mem_limit':'384m','working_dir':'/var/www/moodle',
        'command':['php-fpm','-F'],'secrets':['moodle_db_password'],
        'tmpfs':['/tmp:rw,noexec,nosuid,size=64m,uid=33,gid=33,mode=1777','/var/run:rw,nosuid,size=8m,uid=33,gid=33'],
        'volumes':[bind('code','/var/www/moodle'),bind('moodledata','/var/moodledata',False),
                   bind('pool.conf','/usr/local/etc/php-fpm.d/zz-test.conf'),bind('server.crt','/test-ca.crt')]}
    postgres={**common,'image':images['postgres'],'user':'999:999','mem_limit':'384m',
        'environment':{'POSTGRES_DB':'moodle','POSTGRES_PASSWORD_FILE':'/run/secrets/postgres_admin_password'},
        'secrets':['postgres_admin_password'],
        'tmpfs':['/tmp:rw,noexec,nosuid,size=32m,uid=999,gid=999,mode=1777','/var/run/postgresql:rw,nosuid,size=8m,uid=999,gid=999'],
        'volumes':[bind('postgres-data','/var/lib/postgresql/data',False)]}
    apache={**common,'image':images['apache'],'user':'33:33','mem_limit':'96m',
        'networks':{'isolated':{'aliases':[HOST]}},
        'tmpfs':['/tmp:rw,noexec,nosuid,size=16m,uid=33,gid=33,mode=1777','/usr/local/apache2/logs:rw,nosuid,size=8m,uid=33,gid=33'],
        'volumes':[bind('code','/var/www/moodle'),bind('httpd.conf','/usr/local/apache2/conf/httpd.conf'),
                   bind('server.crt','/usr/local/apache2/conf/tls/server.crt'),bind('server.key','/usr/local/apache2/conf/tls/server.key')]}
    return {'name':'moodle-recovery-test-'+identifier,'services':{'postgres':postgres,'php':php,'apache':apache},
        'networks':{'isolated':{'internal':True,'labels':{LABEL:identifier}}},
        'secrets':{'moodle_db_password':{'file':str(directory/'configuration/secrets/moodle_db_password')},
                   'postgres_admin_password':{'file':str(directory/'postgres-password')}}}


def execute(config,job,directory,commands,*,sleep=time.sleep,clock=time.monotonic):
    directory=Path(directory)
    archives=directory/'archives';archives.mkdir(mode=0o700)
    fields=fetch_moodle(config,job,archives,commands)
    safe_extract(archives/'configuration.tar',directory/'configuration',selected={'compose.yml','secrets/moodle_db_password'})
    safe_extract(archives/'code.tar',directory/'code')
    safe_extract(archives/'moodledata.tar',directory/'moodledata')
    code=directory/'code'
    if not (code/'vendor/autoload.php').is_file() or not (code/'public/login/index.php').is_file():
        raise BackupError('restored_moodle_code_incomplete')
    cfg=code/'config.php'
    if cfg.is_symlink() or not cfg.is_file():raise BackupError('restored_moodle_configuration_invalid')
    text=read_file(cfg,65536).decode()
    pattern=r"(\$CFG->wwwroot\s*=\s*)'([^']+)'"
    roots=re.findall(pattern,text)
    if len(roots)!=1 or roots[0][1]!=fields['URL source']:
        raise BackupError('restored_moodle_url_incompatible')
    cfg.write_text(re.sub(pattern,lambda m:m[1]+"'"+URL+"'",text));cfg.chmod(0o644)
    saved=yaml.safe_load(read_file(directory/'configuration/compose.yml',65536).decode())
    services=saved.get('services',{}) if isinstance(saved,dict) else {}
    if set(services)!={'php','apache','postgres'}:raise BackupError('backup_service_set_invalid')
    images={}
    patterns={'php':r'moodle-pfe/php:moodle-'+re.escape(fields['Moodle'])+r'-[0-9a-f]{12,64}',
              'postgres':r'postgres:16\.[0-9]+-bookworm(?:@sha256:[0-9a-f]{64})?','apache':r'httpd:2\.4\.[0-9]+(?:@sha256:[0-9a-f]{64})?'}
    for service in services:
        image=services[service].get('image')
        if not isinstance(image,str) or not re.fullmatch(patterns[service],image):
            raise BackupError('backup_service_image_incompatible')
        inspected=json_value(commands.run([DOCKER,'image','inspect','--format','{{json .Id}}',image]))
        if not isinstance(inspected,str) or not re.fullmatch('sha256:[0-9a-f]{64}',inspected):
            raise BackupError('test_local_image_unavailable')
        images[service]=inspected
    secret=read_file(directory/'configuration/secrets/moodle_db_password',4096).decode().strip()
    if not re.fullmatch(r'Pfe!1-[0-9a-f]{64}',secret):raise BackupError('backup_application_secret_invalid')
    password=directory/'postgres-password';password.write_text(os.urandom(32).hex());password.chmod(0o444)
    (directory/'configuration/secrets/moodle_db_password').chmod(0o444)
    data=directory/'postgres-data';data.mkdir(mode=0o700);os.chown(data,999,999)
    moodledata=directory/'moodledata'
    from itertools import chain
    for path in chain((moodledata,),moodledata.rglob('*')):
        os.chown(path,33,33,follow_symlinks=False)
        if not path.is_symlink():path.chmod(0o2770 if path.is_dir() else 0o660)
    commands.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-sha256','-nodes','-days','1',
        '-subj','/CN='+HOST,'-addext','subjectAltName=DNS:'+HOST,'-keyout',str(directory/'server.key'),
        '-out',str(directory/'server.crt')],timeout=20)
    (directory/'server.key').chmod(0o444);(directory/'server.crt').chmod(0o444)
    (directory/'httpd.conf').write_text(HTTPD);(directory/'pool.conf').write_text(POOL)
    compose=directory/'compose.json';compose.write_text(json.dumps(compose_config(directory,job['id'],images)))
    dc=[DOCKER,'compose','-p','moodle-recovery-test-'+job['id'],'-f',str(compose)]
    commands.run(dc+['config','--quiet'])
    commands.run(dc+['up','--detach','--no-build','--pull','never','postgres'])
    ready=clock()+180
    while True:
        try:
            commands.run(dc+['exec','-T','--user','999:999','postgres','pg_isready','-h','127.0.0.1','-U','postgres','-d','moodle'],timeout=10)
            break
        except BackupError:
            if clock()>=ready:raise BackupError('test_database_not_ready') from None
            sleep(1)
    role_sql=("BEGIN;CREATE ROLE moodle LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '"+secret+"';"
              "ALTER DATABASE moodle OWNER TO moodle;REVOKE CREATE ON SCHEMA public FROM PUBLIC;COMMIT;")
    with tempfile.TemporaryFile() as source:
        source.write(role_sql.encode());source.seek(0)
        commands.run(dc+['exec','-T','--user','999:999','postgres','psql','-X','-U','postgres','-d','moodle','-v','ON_ERROR_STOP=1'],stdin=source)
    with open(archives/'moodle.pgdump','rb') as source:
        commands.run(dc+['exec','-T','--user','999:999','postgres','pg_restore','--username=postgres','--dbname=moodle',
            '--role=moodle','--no-owner','--no-acl','--single-transaction','--exit-on-error'],stdin=source)
    commands.run(dc+['up','--detach','--no-build','--pull','never','php'])
    commands.run(dc+['exec','-T','--user','33:33','php','php','/var/www/moodle/admin/cli/purge_caches.php'])
    check=("define('CLI_SCRIPT',true);require('/var/www/moodle/config.php');require('/var/www/moodle/public/version.php');"
           "if (!str_starts_with($release,'"+fields['Moodle']+"') || !$DB->record_exists('config',['name'=>'version'])"
           " || !is_writable($CFG->dataroot) || is_writable('/var/www/moodle/config.php')){exit(1);}"
           "$v=['courses'=>$DB->count_records('course'),'users'=>$DB->count_records('user'),'quizzes'=>$DB->count_records('quiz')];"
           "if ($v['courses']<1 || $v['users']<1){exit(1);}echo json_encode($v);")
    counts=json_value(commands.run(dc+['exec','-T','--user','33:33','php','php','-r',check]))
    if (not isinstance(counts,dict) or set(counts)!={'courses','users','quizzes'}
            or any(type(v) is not int or v<0 for v in counts.values())):
        raise BackupError('test_database_verification_invalid')
    commands.run(dc+['up','--detach','--no-build','--pull','never','apache'])
    ready=clock()+60
    while True:
        try:
            commands.run(dc+['exec','-T','--user','33:33','php','php','-r',LOGIN_CHECK],timeout=20)
            break
        except BackupError:
            if clock()>=ready:raise BackupError('test_login_not_verified') from None
            sleep(1)
    return {'status':'restore_validated','production_changed':False,'files_verified':6,
            'archive_check':'passed','database_check':'passed','login_check':'passed',**counts}
