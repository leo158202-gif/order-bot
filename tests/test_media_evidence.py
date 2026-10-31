import json
import sqlite3


def test_media_candidates_never_assigned_by_equal_counts(tmp_path):
    from scripts.gold_audit import audit_media_candidates
    path = tmp_path/'sample.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE orders (id,batch_id,data_json,reasons_json)')
        db.execute('CREATE TABLE messages (msgid,seq,roomid,sender,msgtime,msgtype,content,revoked,batch_id)')
        for oid in (1, 2):
            db.execute('INSERT INTO orders VALUES (?,3,?,?)', (oid,'{}',json.dumps(['包含图片/语音消息，需人工核对'])))
        db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)', [
            ('m1',1,'r1','s',1000,'image','',0,3),
            ('m2',2,'r2','s',1100,'image','',0,3),
            ('m3',3,'r2','s',1200,'text','私密原文不应进入证据报告',0,3)])
    before=path.read_bytes()
    report=audit_media_candidates(path,3)
    assert report['placeholder_ids']==[1,2]
    assert len(report['candidate_groups'])==2
    assert report['count_equal'] is True
    assert report['assignments']==[]
    assert report['historical_mapping_proven'] is False
    assert report['candidate_groups'][1]['source_msgids']==['m2','m3']
    assert '私密原文' not in json.dumps(report,ensure_ascii=False)
    assert path.read_bytes()==before


def test_media_file_link_uses_digest_not_filename_and_omits_raw_fields(tmp_path):
    from hashlib import md5
    from scripts.gold_audit import audit_media_files
    path=tmp_path/'sample.sqlite'; media=tmp_path/'media'; media.mkdir()
    payload=b'actual-image-bytes'; (media/'unrelated-name.bin').write_bytes(payload)
    (media/'m1.jpg').write_bytes(b'wrong-image-bytes!')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE messages (msgid,raw_json,file_path)')
        db.execute('INSERT INTO messages VALUES (?,?,?)',('m1',json.dumps({'image':{
            'md5sum':md5(payload).hexdigest(),'filesize':len(payload),'sdkfileid':'must-not-export'}}),''))
    before=path.read_bytes()
    result=audit_media_files(path,media,['m1','absent'])
    assert result[0]['matching_files']==['unrelated-name.bin']
    assert result[0]['placeholder_assignment_proven'] is False
    assert result[1]['message_found'] is False
    assert 'must-not-export' not in json.dumps(result)
    assert path.read_bytes()==before
