import os
import json
import subprocess
import signal
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional, List
import click


class UIConfigManager:
    """管理 UI 配置的工具类"""
    
    DEFAULT_CONFIG = {
        "agent": {
            "module": "agent",  # 用户必须提供具体的模块路径
            "rootAgent": "root_agent",
            "name": "DP Agent Assistant",
            "description": "AI Assistant powered by DP Agent SDK",
            "welcomeMessage": "欢迎使用 DP Agent Assistant！我可以帮助您进行科学计算、数据分析等任务。",
        },
        "ui": {
            "title": "DP Agent Assistant",
            "theme": "light"
        },
        "files": {
            "output_directory": "./output",
            "watch_directories": ["./output"]
        },
        "websocket": {
            "port": 8000,
            "host": "localhost"
        },
        "server": {
            "port": 50002,
            "host": ["localhost", "127.0.0.1"]
        }
    }
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path) if config_path else Path.cwd() / "agent-config.json"
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件，如果不存在则使用默认配置"""
        if self.config_path.exists():
            with open(self.config_path, 'r') as f:
                user_config = json.load(f)
                # 深度合并用户配置和默认配置
                return self._deep_merge(self.DEFAULT_CONFIG.copy(), user_config)
        return self.DEFAULT_CONFIG.copy()
    
    def _deep_merge(self, base: Dict, update: Dict) -> Dict:
        """深度合并两个字典"""
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                base[key] = self._deep_merge(base[key], value)
            else:
                base[key] = value
        return base
    
    def save_config(self, config_path: Optional[Path] = None):
        """保存配置到文件"""
        save_path = config_path or self.config_path
        with open(save_path, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def update_from_cli(self, **kwargs):
        """从命令行参数更新配置"""
        if kwargs.get('agent'):
            module, _, variable = kwargs['agent'].partition(':')
            self.config['agent']['module'] = module
            if variable:
                self.config['agent']['rootAgent'] = variable
        
        if kwargs.get('port'):
            self.config['server']['port'] = kwargs['port']
        
        if kwargs.get('ws_port'):
            self.config['websocket']['port'] = kwargs['ws_port']
        
        if kwargs.get('host'):
            self.config['server']['host'] = [kwargs['host']]
            self.config['websocket']['host'] = kwargs['host']


class UIProcessManager:
    """管理 UI 相关进程的工具类"""
    
    def __init__(self, ui_dir: Path, config: Dict[str, Any]):
        self.ui_dir = ui_dir
        self.config = config
        self.processes: List[subprocess.Popen] = []
        self._setup_signal_handlers()
    
    def _setup_signal_handlers(self):
        """设置信号处理器以优雅关闭进程"""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """处理终止信号"""
        # Prevent multiple signal handling
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.cleanup()
        # Don't exit here, let the main process handle it
    
    def start_websocket_server(self):
        """启动 WebSocket 服务器"""
        # 统一使用 server.port
        ws_port = self.config.get('server', {}).get('port', 8000)
        
        websocket_script = self.ui_dir / "websocket-server.py"
        if not websocket_script.exists():
            raise FileNotFoundError(f"找不到 websocket-server.py: {websocket_script}")
        
        # 设置环境变量
        env = os.environ.copy()
        env['AGENT_CONFIG_PATH'] = str(self.ui_dir / "config" / "agent-config.temp.json")
        env['USER_WORKING_DIR'] = str(Path.cwd())  # 传递用户工作目录
        env['UI_TEMPLATE_DIR'] = str(self.ui_dir)  # 传递UI模板目录
        # Ensure PYTHONPATH includes the user's working directory
        if 'PYTHONPATH' in env:
            env['PYTHONPATH'] = f"{str(Path.cwd())}:{env['PYTHONPATH']}"
        else:
            env['PYTHONPATH'] = str(Path.cwd())
        
        # 静默启动，将输出重定向到日志文件
        log_file = open(Path.cwd() / "websocket.log", "a")
        process = subprocess.Popen(
            [sys.executable, str(websocket_script)],
            cwd=str(Path.cwd()),  # 在用户工作目录运行
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT
        )
        self.processes.append(process)
        
        # 等待服务器启动
        time.sleep(2)
        
        if process.poll() is not None:
            raise RuntimeError("WebSocket 服务器启动失败")
        
        click.echo(f"🚀 WebSocket 服务器已启动（端口 {ws_port}）")
        click.echo("📝 查看日志: websocket.log")
        
        return process
    
    def start_frontend_server(self, dev_mode: bool = True):
        """启动前端服务器"""
        # 在统一服务架构下，前端访问服务器端口
        server_port = self.config.get('server', {}).get('port', 8000)
        
        ui_path = self.ui_dir / "frontend"
        if not ui_path.exists():
            raise FileNotFoundError(f"找不到 UI 目录: {ui_path}")
        
        # 检查是否有构建好的静态文件
        dist_path = ui_path / "ui-static"
        if dist_path.exists() and not dev_mode:
            # 静态文件由 WebSocket 服务器提供，这里只需要提示
            click.echo(f"✨ Agent UI 已启动: http://localhost:{server_port}")
            click.echo("📁 使用静态文件模式")
            return  # 不需要启动额外的进程，也不添加到进程列表
        
        if not dev_mode and not dist_path.exists():
            click.echo("警告: 未找到构建的静态文件，将使用开发模式")
            dev_mode = True
        
        # 检查是否已安装依赖
        node_modules = ui_path / "node_modules"
        if not node_modules.exists():
            click.echo("检测到未安装前端依赖，正在安装...")
            subprocess.run(["npm", "install"], cwd=str(ui_path), check=True)
        
        # 设置环境变量
        env = os.environ.copy()
        # 开发模式下，前端开发服务器使用不同的端口
        frontend_dev_port = self.config.get('frontend', {}).get('devPort', 3000)
        env['FRONTEND_PORT'] = str(frontend_dev_port)
        # 告诉前端 WebSocket 在哪个端口
        env['VITE_WS_PORT'] = str(server_port)
        
        # 启动命令
        if dev_mode:
            # 开发模式：确保端口不冲突
            cmd = ["npm", "run", "dev"]
            click.echo(f"启动前端开发服务器 (端口: {frontend_dev_port})...")
        else:
            cmd = ["npm", "run", "build"]
            click.echo("构建前端生产版本...")
        
        # 启动前端
        log_file_path = Path.cwd() / "frontend.log"
        with open(log_file_path, "a") as log_file:
            process = subprocess.Popen(
                cmd,
                cwd=str(ui_path),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )
            self.processes.append(process)
            
            # 等待服务器启动
            time.sleep(3)
            
            # 检查进程状态
            if process.poll() is not None:
                # 读取错误日志
                with open(log_file_path, "r") as f:
                    error_log = f.read()
                    if "EADDRINUSE" in error_log:
                        raise RuntimeError(f"端口 {frontend_dev_port} 已被占用")
                    else:
                        raise RuntimeError(f"前端服务器启动失败，请查看 frontend.log 了解详情")
        
        click.echo(f"\n✨ Agent UI 已启动: http://localhost:{frontend_dev_port}\n")
        return process
    
    
    def wait_for_processes(self):
        """等待所有进程结束"""
        try:
            for process in self.processes:
                if process:  # 处理可能的 None
                    process.wait()
        except KeyboardInterrupt:
            # Don't handle here, let it bubble up to main
            raise
    
    def cleanup(self):
        """清理所有进程"""
        if not self.processes:
            return
            
        # First attempt to terminate all processes
        for process in self.processes:
            if process and process.poll() is None:
                try:
                    process.terminate()
                except:
                    pass
        
        # Give processes a short time to terminate
        time.sleep(0.5)
        
        # Force kill any remaining processes
        for process in self.processes:
            if process and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=0.5)
                except:
                    pass
        
        self.processes.clear()


