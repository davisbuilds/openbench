"""Trusted Harbor verifier for registry-bound repair tasks (scheme 4)."""
import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import time

from . import repair_worker
from .harbor_sandbox import read_tree
from .repair_oracles.registry import select, source_names, validate_task_binding
from .sandbox_grading import CandidateFailure, GradingError, freeze_submission


def oracle_module(oracle):
    # The module name comes only from the closed built-in registry.
    return importlib.import_module(oracle.module)


def grade_submission(root, oracle, image, *, timeout=90):
    module=oracle_module(oracle)
    runtime=module.validate_runtime(image)
    try:
        files=read_tree(Path(root))
        permitted=source_names(oracle,files)
        if set(files)!=permitted:
            raise CandidateFailure('source outside trusted oracle policy')
        archive,hashes=repair_worker.source_archive(Path(root),permitted)
        observations,worker=repair_worker.run_worker(image,archive,
            [request for _,_,request in module.cases()],program=module.worker_program(),timeout=timeout)
        graded=module.grade(observations)
        return {**graded,'source_sha256':hashes,'worker':{**worker,'runtime_dependencies':runtime},'oracle_id':oracle.id,'protocol':oracle.protocol}
    except CandidateFailure as exc:
        graded=module.grade([{'ok':False} for _ in module.cases()])
        return {**graded,'candidate_failure':exc.reason,'source_sha256':None,'worker':None,
                'oracle_id':oracle.id,'protocol':oracle.protocol}


class _RegisteredRepairVerifier:
    def __init__(self,*args,worker_image=None,worker_timeout=90,oracle_id=None,**kwargs):
        super().__init__(*args,**kwargs)
        if not worker_image or not oracle_id:
            raise GradingError('worker_image and oracle_id are required')
        self.worker_image=worker_image
        self.worker_timeout=worker_timeout
        self.oracle_id=oracle_id

    async def verify(self):
        from harbor.models.verifier.result import VerifierResult
        start=time.monotonic()
        metadata=self.task.config.metadata
        oracle=select(metadata)
        if self.oracle_id!=oracle.id:
            raise GradingError('locked verifier and task oracle differ')
        root=self.task.paths.task_dir
        binding=validate_task_binding(root,metadata.get('openbench_task_content_digest'))
        with tempfile.TemporaryDirectory(prefix='obench-frozen-') as directory:
            freeze,rejected=await freeze_submission(self.environment,Path(directory))
            validate_task_binding(root,metadata['openbench_task_content_digest'])
            if rejected is None:
                graded=await asyncio.to_thread(grade_submission,Path(directory),oracle,self.worker_image,timeout=self.worker_timeout)
            else:
                module=oracle_module(oracle)
                graded={**module.grade([{'ok':False} for _ in module.cases()]),
                        'candidate_failure':'invalid_source_artifact','source_sha256':None,'worker':None,
                        'oracle_id':oracle.id,'protocol':oracle.protocol}
        validate_task_binding(root,metadata['openbench_task_content_digest'])
        score=graded['score']
        logs=self.trial_paths.verifier_dir
        logs.mkdir(parents=True,exist_ok=True)
        evidence={'schema_version':'openbench-verifier-evidence-v2',
                  'openbench_task_content_digest':metadata['openbench_task_content_digest'],
                  'openbench_harbor_export':metadata['openbench_harbor_export'],
                  'checker_exit':0 if score==1 else 1,'parsed_score':score,'reward':score,
                  'verifier_duration_seconds':time.monotonic()-start}
        (logs/'reward.txt').write_text(str(score)+'\n')
        (logs/'openbench-verifier-evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
        (logs/'sandbox-grading.json').write_text(json.dumps({'freeze':freeze,'grading':graded,'task_binding':binding},indent=2)+'\n')
        return VerifierResult(rewards={'reward':score})


def __getattr__(name):
    if name=='RepairVerifier':
        from harbor.verifier.base import BaseVerifier
        cls=type(name,(_RegisteredRepairVerifier,BaseVerifier),{'__module__':__name__})
        globals()[name]=cls
        return cls
    raise AttributeError(name)
