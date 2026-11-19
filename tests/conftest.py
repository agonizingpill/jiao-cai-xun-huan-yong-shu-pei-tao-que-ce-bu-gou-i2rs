import os,sys,tempfile
os.environ.setdefault('DATABASE_URL','sqlite+pysqlite:///'+tempfile.mkdtemp()+'/t.db')
os.environ.setdefault('JWT_SECRET','test-secret')
os.environ.setdefault('REDIS_URL','redis://localhost:6379/0')
os.environ.setdefault('ARTIFACTS_DIR',tempfile.mkdtemp())
os.environ.setdefault('ANALYSIS_RECOVER_ON_START','false')
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','backend'))
