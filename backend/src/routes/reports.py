from fastapi import APIRouter,Depends,HTTPException
from fastapi.responses import Response
from src.config.database import database
from src.controllers.auth import current,editor,admin
from src.models.run import Run
from src.schemas.requests import RunInput,ReviewInput
from src.services.report import submit,approve
from src.services.export import csv_text,occupancy_csv,rows_csv
from src.services import occupancy_run
from src.utils.serialize import row
router=APIRouter()
def _completed(run_id,db):
 obj=db.get(Run,run_id)
 if not obj: raise HTTPException(404,'报告不存在')
 if obj.status!='completed': raise HTTPException(409,'报告尚未完成')
 return obj
@router.get('/runs')
def runs(db=Depends(database),u=Depends(current)): return [row(x) for x in db.query(Run).order_by(Run.id.desc()).all()]
@router.post('/runs')
def create(b:RunInput,db=Depends(database),u=Depends(editor)): return row(submit(db,u,b))
@router.get('/runs/{id}')
def detail(id:int,db=Depends(database),u=Depends(current)):
 obj=db.get(Run,id)
 if not obj: raise HTTPException(404,'报告不存在')
 return row(obj)
@router.post('/runs/{id}/approve')
def review(id:int,b:ReviewInput,db=Depends(database),u=Depends(admin)): return row(approve(db,u,id,b.note))
@router.get('/runs/{id}/csv')
def download(id:int,db=Depends(database),u=Depends(current)):
 obj=_completed(id,db)
 if u.role=='viewer' and not obj.approved: raise HTTPException(403,'业务阅读者仅可下载已复核报告')
 return Response(csv_text(obj.result),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="report-{id}.csv"'})
@router.get('/runs/{id}/occupancy/csv')
def occupancy_download(id:int,db=Depends(database),u=Depends(current)):
 obj=_completed(id,db)
 if u.role=='viewer' and not obj.approved: raise HTTPException(403,'业务阅读者仅可下载已复核报告')
 if 'occupancy' not in (obj.result or {}): raise HTTPException(409,'占用分摊尚未发布')
 return Response(occupancy_csv(obj.result),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="occupancy-{id}.csv"'})
@router.get('/runs/{id}/occupancy/segments')
def segments(id:int,day:str|None=None,limit:int=200,offset:int=0,db=Depends(database),u=Depends(current)):
 _completed(id,db);limit=min(max(limit,1),1000)
 total,rows=occupancy_run.query_segments(db,id,day,limit,offset)
 return {'total':total,'limit':limit,'offset':offset,'dates':occupancy_run.published_dates(db,id),'rows':rows}
@router.get('/runs/{id}/occupancy/apportion')
def apportion(id:int,db=Depends(database),u=Depends(current)):
 _completed(id,db)
 return {'rows':occupancy_run.query_apportion(db,id)}
@router.get('/runs/{id}/occupancy/trace')
def trace(id:int,natural_key:str|None=None,group:str|None=None,limit:int=100,offset:int=0,db=Depends(database),u=Depends(current)):
 _completed(id,db);limit=min(max(limit,1),1000)
 total,rows=occupancy_run.query_trace(db,id,natural_key,group,limit,offset)
 return {'total':total,'limit':limit,'offset':offset,'rows':rows}
@router.get('/runs/{id}/occupancy/trace/csv')
def trace_csv(id:int,natural_key:str|None=None,group:str|None=None,db=Depends(database),u=Depends(current)):
 obj=_completed(id,db)
 if u.role=='viewer' and not obj.approved: raise HTTPException(403,'业务阅读者仅可下载已复核报告')
 _,rows=occupancy_run.query_trace(db,id,natural_key,group,limit=100000,offset=0)
 return Response(rows_csv(rows),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="occupancy-trace-{id}.csv"'})
