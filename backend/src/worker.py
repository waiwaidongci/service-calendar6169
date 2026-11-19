import time,json,traceback
from pathlib import Path
import redis
from src.config.database import Base,engine,Session
from src.config.settings import REDIS_URL
from src.models import identity,source,run
from src.models.run import Run
from src.services.calculation import calculate
from src.services.occupancy_run import run_occupancy
from src.services.delivery import tick
Base.metadata.create_all(engine)
r=redis.Redis.from_url(REDIS_URL)
while True:
 Path('/tmp/worker-heartbeat').write_text(str(time.time()))
 try:
  item=r.lpop('report-jobs')
  if item:
   with Session() as db:
    job=db.get(Run,int(item))
    # queued 首次执行；running 为中断后续跑（检查点接回跨段在途单，已分摊段不重算）
    if job and job.status in ('queued','running'):
     job.status='running';db.commit()
     try:
      # 原SLA核算照常，不受占用去重模块影响
      result=calculate(job.snapshot['sources'],job.snapshot['policy'])
      # 去重分摊：检查点续跑，发布闸门通过后才写入occupancy；失败不推翻SLA结果
      try:
       occ=run_occupancy(db,job)
       result['occupancy']=occ
       job.error=None
      except Exception as occ_exc:
       # 回滚未通过闸门的发布物；此前已commit的检查点保留，供下次续跑接回
       db.rollback()
       traceback.print_exc()
       result['occupancy_error']='占用去重分摊未通过发布闸门：'+str(occ_exc)
      job.result=result;job.status='completed'
     except Exception as exc: job.status='failed';job.error=str(exc);traceback.print_exc()
     db.commit()
  # 中断续跑：worker 崩溃后残留 running 的任务重新入队，幂等去重，不重复分摊
  with Session() as db:
   queued_ids={int(x) for x in r.lrange('report-jobs',0,-1)}
   for stuck in db.query(Run).filter(Run.status=='running').all():
    if stuck.id not in queued_ids:
     r.rpush('report-jobs',stuck.id)
   tick(db)
 except Exception: traceback.print_exc()
 time.sleep(1)
