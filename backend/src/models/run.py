from sqlalchemy import Column,Integer,String,JSON,Text,Float,Boolean,Index,UniqueConstraint
from src.config.database import Base
class Run(Base):
 __tablename__='sla_runs'
 id=Column(Integer,primary_key=True); name=Column(String(120)); status=Column(String(20)); snapshot=Column(JSON); checksum=Column(String(64)); result=Column(JSON); error=Column(Text); created=Column(Float); approved=Column(Boolean,default=False); review=Column(Text,default='')
class Schedule(Base):
 __tablename__='delivery_schedules'
 id=Column(Integer,primary_key=True); name=Column(String(120)); run_id=Column(Integer); interval_seconds=Column(Integer); next_at=Column(Float); enabled=Column(Boolean,default=True)
class Delivery(Base):
 __tablename__='deliveries'
 id=Column(Integer,primary_key=True); schedule_id=Column(Integer); run_id=Column(Integer); due_key=Column(String(120),unique=True); created=Column(Float); payload=Column(JSON)
class OccupancyCheckpoint(Base):
 """去重分摊扫描检查点：每run一行，中断续跑据此接回跨段在途单。"""
 __tablename__='occupancy_checkpoints'
 id=Column(Integer,primary_key=True); run_id=Column(Integer,unique=True,index=True); state=Column(JSON,nullable=False); updated=Column(Float)
class OccupancySegment(Base):
 """重叠段去重明细：ordinal 在 run 内唯一，重试不重复写入。"""
 __tablename__='occupancy_segments'
 __table_args__=(UniqueConstraint('run_id','ordinal',name='uq_segment_ordinal'),Index('ix_segment_day','run_id','day'))
 id=Column(Integer,primary_key=True); run_id=Column(Integer,index=True); ordinal=Column(Integer); day=Column(String(10)); start_at=Column(String(40)); end_at=Column(String(40)); seconds=Column(Integer); total_active=Column(Integer); groups=Column(JSON)
class OccupancyApportion(Base):
 """日期×小组整数分摊明细，与全局值守恒后才允许发布。"""
 __tablename__='occupancy_apportion'
 __table_args__=(UniqueConstraint('run_id','day','group_name',name='uq_apportion_key'),)
 id=Column(Integer,primary_key=True); run_id=Column(Integer,index=True); day=Column(String(10)); group_name=Column(String(100)); minutes=Column(Integer); exact_minutes=Column(Float); peak_active=Column(Integer)
class OccupancyTrace(Base):
 """逐笔在途单分摊追溯（用于报告页查询），可按run+natural_key定位。"""
 __tablename__='occupancy_traces'
 __table_args__=(Index('ix_trace_lookup','run_id','natural_key'),)
 id=Column(Integer,primary_key=True); run_id=Column(Integer,index=True); natural_key=Column(String(120)); period=Column(String(100)); group_name=Column(String(100)); priority=Column(String(100)); opened=Column(String(40)); closed=Column(String(40)); exact_minutes=Column(Float); solo_minutes=Column(Float); segment_count=Column(Integer)
