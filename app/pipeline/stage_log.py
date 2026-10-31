"""不记录客户正文或凭据的阶段日志。"""
import logging

logger=logging.getLogger('app.pipeline.stages')


def stage(batch_id, name, event, **counts):
    # 仅允许计数/固定分类，失败原因以异常类型记录，不把响应正文/密钥带入日志。
    safe={k:v for k,v in counts.items() if isinstance(v,(int,bool)) or k in ('reason','state')}
    logger.info('batch=%s stage=%s event=%s %s',batch_id,name,event,
                ' '.join(f'{k}={v}' for k,v in safe.items()))
