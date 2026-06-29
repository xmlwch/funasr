# -*- coding: utf-8 -*-
"""ElasticProcessPool — 弹性并发进程池

管理多个 worker 进程的 spawn / 调度 / 缩容:
- in_flight 计数触发主动扩容(不等 worker 异步标 busy)
- min_workers 保活下限,空闲超时只缩到不低于此值
- max_queue 限流,超载直接 RuntimeError 而非等 300s 超时
- stats() 1s 本地缓存,高频 /metrics scrape 不跨进程读 Manager.dict
- start_worker 锁外 spawn(~10ms 不阻塞其他 submit)

典型用法:
    pool = ElasticProcessPool('asr', max_workers=16, idle_timeout=300)
    pool.start_worker()
    pool.wait_ready()
    text = pool.submit('asr', '/path/to/audio.wav')
    pool.shutdown()

【生产改造】从 main.py 拆分,纯并发核心逻辑不依赖 HTTP/security。
"""
import logging
import threading
import time
import uuid
import multiprocessing as mp

from worker import elastic_worker_loop

# 池自身所需的常量 — 与 main._config 同步,保持集中管理更彻底时再合并
INFERENCE_TIMEOUT = 300
WAIT_READY_TIMEOUT = 60
MONITOR_INTERVAL = 10
POLL_INTERVAL = 0.05
STATS_CACHE_TTL = 1.0

logger = logging.getLogger('funasr.pool')


# mp.Manager() 单例 — 避免 N 个池起 N 个 Manager 服务进程
_shared_manager = None


def get_shared_manager():
    global _shared_manager
    if _shared_manager is None:
        _shared_manager = mp.Manager()
    return _shared_manager


class ElasticProcessPool:
    def __init__(self, model_type, max_workers, idle_timeout, max_queue=200, min_workers=1):
        self.model_type = model_type
        self.max_workers = max_workers
        self.idle_timeout = idle_timeout
        self.max_queue = max_queue  # 任务队列上限,防 OOM
        # 最小保活 worker 数,空闲超时后保留多少个不再缩容
        # 生产推荐设 = -prewarm,避免突发流量后冷启动
        self.min_workers = min_workers
        self.task_queue = mp.Queue()
        # Manager.dict 作为跨进程结果通道:worker 写 results[task_id]=res,
        # submit 轮询自己的 task_id 拿到结果。
        # 不能用 mp.Queue + task dict 传递:mp.Queue.__getstate__ 限制 Queue
        # 只能通过 Process(args=...) 直接传,不能通过其他 Queue 间接传(spawn 下)
        self.manager = get_shared_manager()
        self.worker_state = self.manager.dict()
        self.results = self.manager.dict()
        self.workers = {}
        self.lock = threading.Lock()
        self.is_shutting_down = False
        # in_flight:当前在飞任务数(已派发未拿结果)
        # 比 busy 状态更准——worker 标 busy 有 ms 级延迟,in_flight 立即可见
        # 用它做主动扩容:in_flight >= alive 时立刻 spawn 新 worker
        self.in_flight = 0
        self.scale_events = 0  # 扩容次数,metrics 观测用

        self.monitor_thread = threading.Thread(target=self._monitor_workers, daemon=True)
        self.monitor_thread.start()
        # stats() 本地缓存 — 高频 /metrics scrape 时避免跨进程读 Manager.dict
        self._stats_cache = None
        self._stats_cache_time = 0.0

    def start_worker(self):
        # 本方法必须在调用方不持锁时才进(锁外 spawn)
        # 因为 mp.Process.start() 内部阻塞 ~10ms,在锁内会阻塞其他 submit
        if len(self.workers) >= self.max_workers: return

        # os.getpid() 占位符 0 — worker.py 里用 real_pid 覆盖
        # results 是 Manager.dict 代理,worker 通过它写结果
        p = mp.Process(target=elastic_worker_loop,
                       args=(self.task_queue, self.results, self.worker_state, 0,
                             self.idle_timeout, self.model_type, self.min_workers))
        p.start()
        self.workers[p.pid] = p
        logger.info("[%s Pool] 启动新 Worker (PID: %d),当前 %s 池总数: %d",
                    self.model_type.upper(), p.pid, self.model_type, len(self.workers))

    def wait_ready(self, timeout=WAIT_READY_TIMEOUT):
        """轮询等待本池有 worker 进入 idle 状态(模型加载完成)"""
        name = self.model_type.upper()
        for i in range(timeout):
            time.sleep(1)
            if any(s.get('status') == 'idle' for s in self.worker_state.values()):
                logger.info("✓ %s 池就绪", name)
                return True
            if i > 0 and i % 5 == 0:
                logger.info("%s 池: 已等待 %d 秒...", name, i)
        logger.warning("%s 池等待超时,模型可能加载失败!", name)
        return False

    def submit(self, func_name, path):
        if self.is_shutting_down:
            raise RuntimeError("服务正在关闭,拒绝新请求")

        task_id = uuid.uuid4().hex

        # M4:need_scale 标记 — start_worker 移到锁外
        try:
            need_scale = False
            with self.lock:
                # 先占 in_flight 再判断扩容——
                # 这样并发提交时每个请求都立刻看到自己在飞,
                # 触发扩容不等 worker 异步标 busy
                self.in_flight += 1
                alive = sum(1 for p in self.workers.values() if p.is_alive())
                # 队列过载保护:in_flight 上限 = alive + max_queue
                if self.in_flight > alive + self.max_queue:
                    # finally 会统一递减,不在这里 -=1
                    raise RuntimeError(f"队列已满({alive} worker / {self.max_queue} 排队上限)")
                # 主动扩容:in_flight 接近 alive 时标记,锁外 spawn
                if alive < self.max_workers and self.in_flight >= alive:
                    need_scale = True
                    self.scale_events += 1
                # task dict 只放可序列化的简单数据
                self.task_queue.put({'id': task_id, 'func': func_name, 'path': path})

            if need_scale:
                self.start_worker()  # 锁外 spawn

            # 轮询 self.results 等 worker 写入,task_id 唯一不会拿错
            start_time = time.time()
            while True:
                if self.is_shutting_down:
                    raise RuntimeError("服务正在关闭,推理被中断")
                if task_id in self.results:
                    data = self.results.pop(task_id)
                    if isinstance(data, Exception): raise data
                    return data
                if time.time() - start_time > INFERENCE_TIMEOUT:
                    raise TimeoutError("推理超时")
                time.sleep(POLL_INTERVAL)
        finally:
            # H2:清理 results,worker 晚到写入也无所谓 — 下次 submit 也会清理
            # 防 TimeoutError 后 worker 仍写回结果造成的内存泄漏
            self.results.pop(task_id, None)
            # 无论成功 / 队列满 / 超时 / 关闭,都只递减一次
            with self.lock:
                self.in_flight -= 1

    def stats(self):
        """池状态快照 — 用于 /metrics 端点和日志。线程安全。"""
        # M5:1 秒本地缓存,避免高频 scrape 时跨进程 Manager.dict 读
        now = time.time()
        if self._stats_cache and now - self._stats_cache_time < STATS_CACHE_TTL:
            return self._stats_cache
        with self.lock:
            # 用 items() 一次拿到 (pid, state) 快照,避免两次远程调用之间被改
            state_snapshot = dict(self.worker_state.items())
            states = list(state_snapshot.values())
            alive = sum(1 for p in self.workers.values() if p.is_alive())
            alive_pids = {p.pid for p in self.workers.values() if p.is_alive()}
            # loading:已 spawn 但 worker_state 还没写入(模型加载中)
            loaded_pids = {pid for pid in state_snapshot.keys() if pid in alive_pids}
            loading = alive - len(loaded_pids)
            result = {
                'model_type': self.model_type,
                'alive': alive,
                'max': self.max_workers,
                'min': self.min_workers,
                'in_flight': self.in_flight,
                'idle': sum(1 for s in states if s.get('status') == 'idle'),
                'busy': sum(1 for s in states if s.get('status') == 'busy'),
                'loading': loading,
                'dead': sum(1 for s in states if s.get('status') == 'dead'),
                'scale_events': self.scale_events,
            }
            self._stats_cache = result
            self._stats_cache_time = now
            return result

    def shutdown(self):
        self.is_shutting_down = True
        logger.info("[Pool] 正在发送退出信号 (毒丸)...")
        for _ in range(self.max_workers):
            self.task_queue.put(None)

        logger.info("[Pool] 等待 Worker 进程退出...")
        with self.lock:
            for pid, p in list(self.workers.items()):
                p.join(timeout=5)
                if p.is_alive():
                    logger.warning("[Pool] Worker (PID: %d) 未响应,强制终止", pid)
                    p.terminate()
                    p.join(timeout=2)
            self.workers.clear()

        # 不在池 shutdown 里关 Manager — Manager 是多池共享的,
        # 第一个池关掉 Manager 会让其他池的 worker 写 worker_state 失败。
        # 交给主进程退出时 OS 回收 Manager 子进程。
        logger.info("[Pool] 所有 Worker 已安全退出。")

    def _monitor_workers(self):
        while True:
            time.sleep(MONITOR_INTERVAL)
            with self.lock:
                dead_pids = [pid for pid in self.workers if not self.workers[pid].is_alive()]
                for pid in dead_pids:
                    self.workers.pop(pid, None)
                    self.worker_state.pop(pid, None)
                # 启动时已通过 preflight_check_models 校验过模型文件存在,
                # 所以这里 worker 死掉通常是运行时问题(OOM、bug 等),系统自愈:
                # 下次 submit 看到 alive 不足会触发 start_worker。
