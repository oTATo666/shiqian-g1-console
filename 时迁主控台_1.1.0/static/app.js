let currentTab = 'overview';
let codesData = [];

// 透明挂载 X-Shiqian-Token 请求头（仅驻留当前浏览器会话内存 sessionStorage，绝不落盘）
const _SHIQIAN_TOKEN_STORAGE_KEY = 'shiqian_bridge_token';
let _lastAuthPromptAt = 0;
(function installShiqianAuthFetch() {
    const rawFetch = window.fetch.bind(window);
    window.fetch = function(input, init) {
        const urlStr = typeof input === 'string' ? input : ((input && input.url) || '');
        const isLocalApi = urlStr.startsWith('/api/') || urlStr.startsWith(window.location.origin + '/api/');
        let nextInit = init ? Object.assign({}, init) : {};
        if (isLocalApi) {
            const tok = (sessionStorage.getItem(_SHIQIAN_TOKEN_STORAGE_KEY) || '').trim();
            if (tok) {
                const hdrs = new Headers(nextInit.headers || {});
                if (!hdrs.has('X-Shiqian-Token')) {
                    hdrs.set('X-Shiqian-Token', tok);
                }
                nextInit.headers = hdrs;
            }
        }
        return rawFetch(input, nextInit).then(resp => {
            if (isLocalApi && (resp.status === 401 || resp.status === 403)) {
                resp.clone().json().then(j => {
                    if (j && j.auth_required && Date.now() - _lastAuthPromptAt > 4000) {
                        _lastAuthPromptAt = Date.now();
                        const drawer = document.getElementById('promise-drawer');
                        if (drawer && !drawer.classList.contains('open')) {
                            drawer.classList.add('open');
                        }
                        const inp = document.getElementById('prm-session-token');
                        if (inp) inp.focus();
                        showToast(j.msg || '需要验证 X-Shiqian-Token 跨端操控口令', 'error');
                    }
                }).catch(() => {});
            }
            return resp;
        });
    };
})();

function switchTab(tabId) {
    document.querySelectorAll('main > div > section').forEach(el => el.classList.add('hidden'));
    document.getElementById(`tab-${tabId}`).classList.remove('hidden');
    const mobSel = document.getElementById('prm-mobile-tab');
    if (mobSel && mobSel.value !== tabId) mobSel.value = tabId;
    
    document.querySelectorAll('nav button').forEach(btn => {
        btn.classList.remove('bg-indigo-50', 'text-indigo-600');
        btn.classList.add('text-slate-600');
    });
    const activeBtn = document.getElementById(`nav-${tabId}`);
    if (activeBtn) {
        activeBtn.classList.remove('text-slate-600');
        activeBtn.classList.add('bg-indigo-50', 'text-indigo-600');
    }
    currentTab = tabId;
    if (tabId === 'redeem') loadRedeemCodes();
    if (tabId === 'settings') loadConfigToForm();
    if (tabId === 'processes') fetchLogs();
    if (tabId === 'sensors') startSensorPolling(); else stopSensorPolling();
    if (tabId === 'motion') startMotionPolling(); else stopMotionPolling();
    if (tabId === 'control') startReceptionPolling(); else stopReceptionPolling();
}

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    const bgColor = type === 'success' ? 'bg-slate-900 text-white' : 'bg-rose-600 text-white';
    toast.className = `${bgColor} px-4 py-3 rounded-xl text-sm font-medium shadow-lg transition-all transform translate-y-2 opacity-0 flex items-center space-x-2`;
    toast.innerHTML = `<span>${message}</span>`;
    container.appendChild(toast);
    
    setTimeout(() => { toast.classList.remove('translate-y-2', 'opacity-0'); }, 10);
    setTimeout(() => {
        toast.classList.add('translate-y-2', 'opacity-0');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function checkHealth() {
    refreshLlmBadge();
    refreshPromiseStatus(false);
    fetch('/api/health')
        .then(res => res.json())
        .then(data => {
            if (data.ok) {
                const info = data.data;
                if (!info.dependencies_installed) {
                    document.getElementById('health-banner').classList.remove('hidden');
                } else {
                    document.getElementById('health-banner').classList.add('hidden');
                }
                if (info.auth) {
                    updateAuthAndNetworkBadges(info.auth);
                }
            }
        });

    fetch('/api/status')
        .then(res => res.json())
        .then(data => {
            if (data.ok) {
                const st = data.data;
                const anyAlive = Object.values(st.modules).some(m => m.alive);
                
                const dot = document.getElementById('global-status-dot');
                const txt = document.getElementById('global-status-text');
                if (anyAlive) {
                    dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse';
                    txt.innerText = '系统运行中';
                } else {
                    dot.className = 'w-2.5 h-2.5 rounded-full bg-slate-300';
                    txt.innerText = '系统已停止';
                }

                const sysDot = document.getElementById('card-sys-dot');
                const sysTxt = document.getElementById('card-sys-text');
                if (anyAlive) {
                    sysDot.className = 'w-3 h-3 rounded-full bg-emerald-500 animate-pulse';
                    sysTxt.innerText = '正常运行';
                    document.getElementById('btn-quick-start').innerText = '停止系统';
                    document.getElementById('btn-quick-start').onclick = stopSystem;
                } else {
                    sysDot.className = 'w-3 h-3 rounded-full bg-slate-300';
                    sysTxt.innerText = '未启动';
                    document.getElementById('btn-quick-start').innerText = '一键启动系统';
                    document.getElementById('btn-quick-start').onclick = startSystem;
                }

                const g1Dot = document.getElementById('card-g1-dot');
                const g1Txt = document.getElementById('card-g1-text');
                if (st.g1_connected) {
                    g1Dot.className = 'w-3 h-3 rounded-full bg-emerald-500';
                    g1Txt.innerText = '在线连通';
                } else {
                    g1Dot.className = 'w-3 h-3 rounded-full bg-slate-300';
                    g1Txt.innerText = '离线 / 未连通';
                }

                updateModuleBadge('asr', st.modules.asr);
                updateModuleBadge('redeem', st.modules.redeem);
                updateModuleBadge('mock', st.modules.mock);
                updateModuleBadge('sensor', st.modules.sensor);
                updateModuleBadge('motion', st.modules.motion);
                updateModuleBadge('vision', st.modules.vision);
                updateSayState(st.modules.asr);
                if (currentTab === 'processes') fetchLogs();
                if (st.sensor && currentTab !== 'sensors') {
                    // 概览页也能看到传感器桥的真实链路状态
                    const g1Txt = document.getElementById('card-g1-text');
                    if (g1Txt && st.sensor.ready) {
                        g1Txt.innerText = `在线连通 · ${st.sensor.hz} Hz`;
                    } else if (g1Txt && st.sensor.bridge) {
                        g1Txt.innerText = `桥已启动，机器人未就绪${st.sensor.not_ready_reason ? ' · ' + st.sensor.not_ready_reason : ''}`;
                    }
                }
                updateMascotFromStatus(st);
            }
        });
}

function refreshLlmBadge() {
    fetch('/api/llm/status').then(r => r.json()).then(d => {
        const dot = document.getElementById('mod-llm-dot');
        const txt = document.getElementById('mod-llm-txt');
        if (!dot || !txt) return;
        const ok = !!d.ok;
        dot.className = 'w-2.5 h-2.5 rounded-full ' + (ok ? 'bg-emerald-500' : 'bg-slate-300');
        txt.innerText = ok ? '就绪' : '未启动';
        txt.className = 'text-sm font-medium ' + (ok ? 'text-emerald-600' : 'text-slate-700');
    }).catch(() => { });
}

function updateSayState(mod) {
    const el = document.getElementById('say-state');
    if (!el) return;
    if (mod && mod.state === 'running') {
        el.innerText = mod.pid ? `运行中 · PID ${mod.pid}` : '运行中';
        el.className = 'text-xs text-emerald-600 font-medium shrink-0';
    } else if (mod && (mod.state === 'failed' || mod.state === 'missing')) {
        el.innerText = '语音主程序启动失败';
        el.className = 'text-xs text-rose-600 font-medium shrink-0';
    } else if (mod && mod.state === 'starting') {
        el.innerText = '正在启动…';
        el.className = 'text-xs text-amber-600 font-medium shrink-0';
    } else {
        el.innerText = '语音主程序未运行';
        el.className = 'text-xs text-slate-400 shrink-0';
    }
}

function updateModuleBadge(name, mod) {
    const alive = !!(mod && mod.alive);
    const state = (mod && mod.state) || 'stopped';
    const pid = mod && mod.pid;

    let label = '未运行';
    let textCls = 'text-slate-700';
    let dotCls = 'bg-slate-300';
    if (alive && mod && mod.ready === false) {
        const why = mod.not_ready_reason ? ` · ${mod.not_ready_reason}` : '';
        label = `桥已启动，机器人未就绪${why}`;
        textCls = 'text-amber-600';
        dotCls = 'bg-amber-400';
    } else if (alive) {
        label = '运行中'; textCls = 'text-emerald-600'; dotCls = 'bg-emerald-500 animate-pulse';
    } else if (state === 'failed') {
        label = '启动失败'; textCls = 'text-rose-600'; dotCls = 'bg-rose-500';
    } else if (state === 'starting') {
        label = '启动中'; textCls = 'text-amber-600'; dotCls = 'bg-amber-400 animate-pulse';
    } else if (state === 'stopping') {
        label = '停止中'; textCls = 'text-slate-500'; dotCls = 'bg-amber-400';
    } else if (state === 'missing') {
        label = '未找到脚本'; textCls = 'text-slate-500'; dotCls = 'bg-slate-300';
    }

    const dot = document.getElementById(`mod-${name}-dot`);
    const txt = document.getElementById(`mod-${name}-txt`);
    if (dot && txt) {
        dot.className = `w-2.5 h-2.5 rounded-full ${dotCls}`;
        txt.innerText = label;
        txt.className = `text-sm font-medium ${textCls}`;
    }
    const pDot = document.getElementById(`proc-${name}-dot`);
    const pPid = document.getElementById(`proc-${name}-pid`);
    if (pDot && pPid) {
        pDot.className = `w-2.5 h-2.5 rounded-full ${dotCls}`;
        if (alive) {
            pPid.innerText = `PID: ${pid}`;
        } else if (state === 'failed') {
            pPid.innerText = `已退出（退出码 ${mod.exit_code}）`;
        } else {
            pPid.innerText = 'PID: 未运行';
        }
    }
}

function installEnv() {
    showToast('正在安装依赖包，请稍候...', 'success');
    fetch('/api/install', { method: 'POST' })
        .then(res => res.json())
        .then(data => {
            if (data.ok) {
                showToast('依赖安装成功！');
                document.getElementById('health-banner').classList.add('hidden');
            } else {
                showToast(data.msg || '安装失败', 'error');
            }
        });
}

function startSystem() {
    fetch('/api/start', { method: 'POST' }).then(res => res.json()).then(data => {
        showToast(data.msg);
        checkHealth();
    });
}
function stopSystem() {
    if (!confirm('确定要停止所有后台模块吗？')) return;
    fetch('/api/stop', { method: 'POST' }).then(res => res.json()).then(data => {
        showToast(data.msg);
        checkHealth();
    });
}
function restartSystem() {
    fetch('/api/restart', { method: 'POST' }).then(res => res.json()).then(data => {
        showToast(data.msg);
        checkHealth();
    });
}
function toggleSystem() {
    const btn = document.getElementById('btn-quick-start');
    if (btn.innerText.includes('启动')) {
        startSystem();
    } else {
        stopSystem();
    }
}
function restartModule(name) {
    fetch(`/api/module/${name}/restart`, { method: 'POST' }).then(res => res.json()).then(data => {
        showToast(data.msg);
        checkHealth();
    });
}

function fetchLogs() {
    fetch('/api/logs').then(res => res.json()).then(data => {
        if (data.ok) {
            const container = document.getElementById('log-container');
            container.innerHTML = data.data.map(log => `<div>${log}</div>`).join('');
            container.scrollTop = container.scrollHeight;
        }
    });
}

function loadRedeemCodes() {
    fetch('/api/redeem/list').then(res => res.json()).then(data => {
        if (data.ok) {
            codesData = data.data.codes;
            document.getElementById('redeem-today-badge').innerText = `${data.data.today_count} 人次`;
            document.getElementById('card-redeem-count').innerText = data.data.today_count;
            renderCodesTable(codesData);
        }
    });
}
function renderCodesTable(list) {
    const tbody = document.getElementById('codes-table-body');
    tbody.innerHTML = list.map(item => `
        <tr class="hover:bg-slate-50/50">
            <td class="py-3 px-6 font-mono font-medium text-slate-800">${item.code}</td>
            <td class="py-3 px-6">
                <span class="px-2.5 py-1 rounded-full text-xs font-medium ${item.status === 'redeemed' ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-100 text-slate-600'}">
                    ${item.status === 'redeemed' ? '已核销' : (item.status === 'void' ? '已作废' : '未使用')}
                </span>
            </td>
            <td class="py-3 px-6 text-slate-500 text-xs">${item.time || '-'}</td>
            <td class="py-3 px-6 text-right">
                <button onclick="voidCode('${item.code}')" class="text-xs text-rose-600 hover:text-rose-700 font-medium">作废</button>
            </td>
        </tr>
    `).join('');
}
function filterCodes() {
    const keyword = document.getElementById('redeem-search').value.toLowerCase();
    const filtered = codesData.filter(i => i.code.toLowerCase().includes(keyword));
    renderCodesTable(filtered);
}
function doRedeem() {
    const code = document.getElementById('redeem-input').value.trim();
    if (!code) {
        showToast('请输入兑换码', 'error');
        return;
    }
    fetch('/api/redeem', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code })
    }).then(res => res.json()).then(data => {
        if (data.ok) {
            showToast(data.msg, 'success');
            document.getElementById('redeem-input').value = '';
            loadRedeemCodes();
        } else {
            showToast(data.msg, 'error');
        }
    });
}
function voidCode(code) {
    if (!confirm(`确定要作废兑换码 ${code} 吗？`)) return;
    fetch('/api/redeem/void', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code })
    }).then(res => res.json()).then(data => {
        showToast(data.msg);
        loadRedeemCodes();
    });
}
function exportCSV() {
    window.location.href = '/api/redeem/export';
}

function toggleMockMode(checkbox) {
    if (checkbox.checked) {
        fetch('/api/demo/start', { method: 'POST' }).then(res => res.json()).then(data => {
            showToast(data.msg);
            checkHealth();
        });
    } else {
        fetch('/api/demo/stop', { method: 'POST' }).then(res => res.json()).then(data => {
            showToast(data.msg);
            checkHealth();
        });
    }
}
function loadConfigToForm() {
    fetch('/api/config').then(res => res.json()).then(data => {
        if (data.ok) {
            const cfg = data.data;
            const form = document.getElementById('settings-form');
            if (cfg.g1) {
                form.elements['g1_roboturl'].value = cfg.g1.roboturl || '';
                form.elements['g1_wsurl'].value = cfg.g1.wsurl || '';
            }
            if (cfg.server) {
                form.elements['server_consoleport'].value = cfg.server.consoleport || 5000;
                form.elements['server_redeemport'].value = cfg.server.redeemport || 5001;
            }
            if (cfg.llm) {
                form.elements['llm_apiurl'].value = cfg.llm.apiurl || '';
                form.elements['llm_model'].value = cfg.llm.model || '';
                form.elements['llm_apikey'].value = cfg.llm.apikey || '';
            }
            if (cfg.redeem) {
                form.elements['redeem_adminpassword'].value = cfg.redeem.adminpassword || '';
            }
            if (cfg.behavior) {
                form.elements['behavior_actioncooldownsec'].value = cfg.behavior.actioncooldownsec || 2;
                form.elements['behavior_requestttlsec'].value = cfg.behavior.requestttlsec || 60;
                form.elements['behavior_tts_volume'].value = cfg.behavior.tts_volume || 80;
            }
            if (form.elements['sensor_python']) {
                form.elements['sensor_python'].value = (cfg.sensor && cfg.sensor.python) || '';
            }
            if (cfg.avatar) {
                if (form.elements['avatar_enabled']) form.elements['avatar_enabled'].checked = cfg.avatar.enabled !== false;
                if (form.elements['avatar_boot_enabled']) form.elements['avatar_boot_enabled'].checked = cfg.avatar.boot_enabled !== false;
                if (form.elements['avatar_boot_duration_ms']) form.elements['avatar_boot_duration_ms'].value = cfg.avatar.boot_duration_ms || 4200;
            }
            const pb = cfg.promise_bridge || {};
            if (form.elements['promise_url']) form.elements['promise_url'].value = pb.url || 'http://127.0.0.1:7100';
            if (form.elements['promise_token_env']) form.elements['promise_token_env'].value = pb.token_env || 'SHIQIAN_BRIDGE_TOKEN';
            if (form.elements['promise_bind_host']) form.elements['promise_bind_host'].value = pb.bind_host || '127.0.0.1';
            if (form.elements['promise_auth_header']) form.elements['promise_auth_header'].value = pb.promise_auth_header || '';
            if (form.elements['promise_enabled']) form.elements['promise_enabled'].checked = pb.enabled !== false;
            if (form.elements['promise_allow_tailscale']) form.elements['promise_allow_tailscale'].checked = pb.allow_tailscale !== false;
        }
    });
}
function saveConfig(e) {
    e.preventDefault();
    const form = e.target;
    const newConfig = {
        g1: {
            roboturl: form.elements['g1_roboturl'].value,
            wsurl: form.elements['g1_wsurl'].value,
            apikey: form.elements['llm_apikey'].value,
            actionendpoint: "/triggeraction/{actionid}",
            actionstatusendpoint: "/actionstatus/{requestid}"
        },
        server: {
            consoleport: parseInt(form.elements['server_consoleport'].value) || 5000,
            redeemport: parseInt(form.elements['server_redeemport'].value) || 5001,
            mock_port: 5002
        },
        llm: {
            apiurl: form.elements['llm_apiurl'].value,
            apikey: form.elements['llm_apikey'].value,
            model: form.elements['llm_model'].value
        },
        redeem: {
            adminpassword: form.elements['redeem_adminpassword'].value,
            logenabled: true
        },
        behavior: {
            actioncooldownsec: parseInt(form.elements['behavior_actioncooldownsec'].value) || 2,
            requestttlsec: parseInt(form.elements['behavior_requestttlsec'].value) || 60,
            callbacktimeoutsec: 5,
            tts_volume: parseInt(form.elements['behavior_tts_volume'].value) || 80
        },
        sensor: {
            python: form.elements['sensor_python'] ? form.elements['sensor_python'].value.trim() : ''
        },
        avatar: {
            enabled: form.elements['avatar_enabled'] ? !!form.elements['avatar_enabled'].checked : true,
            boot_enabled: form.elements['avatar_boot_enabled'] ? !!form.elements['avatar_boot_enabled'].checked : true,
            boot_duration_ms: parseInt(form.elements['avatar_boot_duration_ms'] && form.elements['avatar_boot_duration_ms'].value) || 4200
        },
        promise_bridge: {
            enabled: form.elements['promise_enabled'] ? !!form.elements['promise_enabled'].checked : true,
            url: (form.elements['promise_url'] && form.elements['promise_url'].value.trim()) || 'http://127.0.0.1:7100',
            token_env: (form.elements['promise_token_env'] && form.elements['promise_token_env'].value.trim()) || 'SHIQIAN_BRIDGE_TOKEN',
            bind_host: (form.elements['promise_bind_host'] && form.elements['promise_bind_host'].value.trim()) || '127.0.0.1',
            promise_auth_header: (form.elements['promise_auth_header'] && form.elements['promise_auth_header'].value) || '',
            allow_tailscale: form.elements['promise_allow_tailscale'] ? !!form.elements['promise_allow_tailscale'].checked : true
        }
    };

    fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newConfig)
    }).then(res => res.json()).then(data => {
        if (data.ok) {
            showToast('配置保存成功！端口或监听网卡改动需重启服务生效。');
            refreshPromiseStatus(true);
        } else {
            showToast(data.msg || '保存失败', 'error');
        }
    });
}
function rollbackConfig() {
    if (!confirm('确定要回滚到上一次保存的配置吗？')) return;
    fetch('/api/rollback_config', { method: 'POST' }).then(res => res.json()).then(data => {
        if (data.ok) {
            showToast(data.msg);
            loadConfigToForm();
        } else {
            showToast(data.msg, 'error');
        }
    });
}

function detectBridgePython() {
    const box = document.getElementById('sensor-python-detect-result');
    if (box) {
        box.classList.remove('hidden');
        box.innerText = '正在检测 Bridge Python 解释器...';
    }
    fetch('/api/sensors/info?force=1')
        .then(res => res.json())
        .then(res => {
            if (!res.ok || !res.data) {
                if (box) box.innerText = '检测失败：' + (res.msg || '未知错误');
                showToast(res.msg || '检测失败', 'error');
                return;
            }
            const d = res.data;
            const statusText = d.python_ok
                ? `✅ 可用：${d.python || ''}`
                : '❌ 未找到可用解释器';
            const cands = (d.candidates && d.candidates.length)
                ? d.candidates.map(c => '  - ' + c).join('\n')
                : '  （无）';
            if (box) {
                box.innerText = `${statusText}\n说明：${d.python_msg || ''}\n候选路径（${(d.candidates || []).length} 个）：\n${cands}`;
            }
            showToast(d.python_ok ? 'Bridge Python 检测通过' : 'Bridge Python 检测未通过', d.python_ok ? 'success' : 'error');
        })
        .catch(() => {
            if (box) box.innerText = '检测请求失败：主控台无响应';
            showToast('检测请求失败', 'error');
        });
}

/* ==================================================================
   机器人传感页
   ------------------------------------------------------------------
   页面只跟主控台说话（/api/sensors/*），主控台再转发到 127.0.0.1:5011
   的传感器桥。轮询间隔 600ms：关节 29 路 + IMU 足够流畅，又不会打满。
   ================================================================== */
let sensorTimer = null;
let sensorJointsBuilt = 0;
let videoStreaming = false;

function setPill(id, text, state) {
    const el = document.getElementById(id);
    if (!el) return;
    const dot = el.querySelector('.g1-dot');
    if (dot) dot.className = 'g1-dot' + (state === 'on' ? ' on' : state === 'warn' ? ' warn' : state === 'off' ? ' off' : '');
    const label = el.querySelector('span');
    if (label) label.innerText = text;
}

function startSensorPolling() {
    if (sensorTimer) return;
    pollSensors();
    sensorTimer = setInterval(pollSensors, 600);
}

function stopSensorPolling() {
    if (sensorTimer) {
        clearInterval(sensorTimer);
        sensorTimer = null;
    }
}

function pollSensors() {
    if (currentTab !== 'sensors') return;
    fetch('/api/sensors/state')
        .then(res => res.json())
        .then(data => {
            if (!data.ok) return sensorDown(data.msg || '传感器桥未启动');
            renderSensors(data);
        })
        .catch(() => sensorDown('传感器桥未启动'));
}

function sensorDown(msg) {
    setPill('g1-link-pill', '桥未启动', 'off');
    setPill('g1-video-pill', '不可用', 'off');
    setPill('g1-mic-pill', '不可用', 'off');
    const note = document.getElementById('g1-video-note');
    if (note) note.innerText = msg + ' · 点右上角「启动传感器桥」';
    videoStreaming = false;
    const img = document.getElementById('g1-video-img');
    if (img) { img.style.display = 'none'; img.removeAttribute('src'); }
    const empty = document.getElementById('g1-video-empty');
    if (empty) empty.style.display = 'flex';
}

function renderSensors(d) {
    // ---- 链路 ----
    const dds = d.dds || {};
    const link = d.link || {};
    if (dds.ready && link.lowstate_age_ms !== null && link.lowstate_age_ms < 2000) {
        setPill('g1-link-pill', `就绪 · DDS ${link.lowstate_hz} Hz`, 'on');
    } else if (!dds.ready) {
        const why = dds.error ? `DDS 未绑定（${dds.error}）` : 'DDS 未绑定';
        setPill('g1-link-pill', `桥已启动，机器人未就绪 · ${why}`, 'warn');
    } else {
        setPill('g1-link-pill', '桥已启动，机器人未就绪 · 无 lowstate', 'warn');
    }

    // ---- 视频 ----
    const v = d.video || {};
    const img = document.getElementById('g1-video-img');
    const empty = document.getElementById('g1-video-empty');
    const note = document.getElementById('g1-video-note');
    document.getElementById('g1-video-source').innerText = v.source === 'dds' ? 'rt/frontvideostream' : (v.source === 'url' ? '外部源' : '未配置');
    document.getElementById('g1-video-hz').innerText = v.hz ? `${v.hz} fps` : '—';
    document.getElementById('g1-video-codec').innerText = v.codec || '—';
    if (v.has_frame) {
        setPill('g1-video-pill', `${v.hz || 0} fps`, 'on');
        if (empty) empty.style.display = 'none';
        if (img) {
            img.style.display = 'block';
            if (!videoStreaming) {
                videoStreaming = true;
                img.src = '/api/sensors/video.mjpg?ts=' + Date.now();
            }
        }
    } else {
        setPill('g1-video-pill', v.source === 'none' ? '未订阅' : '无画面', 'warn');
        if (empty) empty.style.display = 'flex';
        if (note) {
            let msg = v.note || '';
            if (msg.indexOf('Timeout') >= 0) {
                msg = '视频源超时：相机可能掉线或没枚举到（检查头部相机排线/供电）。桥每 2 秒自动重连，插稳后画面会自己回来。';
            } else if (!msg) {
                msg = '机载相机没有出图：检查头部相机排线/供电，或 rt/frontvideostream 是否有发布者';
            }
            note.innerText = msg;
        }
        if (img) { img.style.display = 'none'; img.removeAttribute('src'); }
        videoStreaming = false;
    }

    // ---- 麦克风 ----
    const m = d.mic || {};
    const active = !!m.active;
    setPill('g1-mic-pill', active ? (m.age_ms !== null && m.age_ms < 1500 ? '采集中' : '等待音频') : '未采集', active ? 'on' : '');
    const bar = document.getElementById('g1-mic-bar');
    const pct = Math.min(100, Math.round((m.rms || 0) * 420));
    if (bar) bar.style.width = pct + '%';
    const db = m.rms > 0 ? (20 * Math.log10(m.rms)).toFixed(1) + ' dB' : '-∞ dB';
    document.getElementById('g1-mic-db').innerText = db;
    document.getElementById('g1-mic-packets').innerText = m.packets || 0;
    let asr = '—';
    const last = (d.audio || []).slice(-1)[0] || '';
    if (last.indexOf('play_state') >= 0) {
        asr = last.indexOf('"play_state":1') >= 0 ? '播报中' : '空闲';
    } else if (last.trim().startsWith('{')) {
        try {
            const j = JSON.parse(last);
            asr = j.text || j.asr || j.result || ('语音服务 ' + Object.keys(j).slice(0, 3).join('/'));
        } catch (e) {
            asr = '语音服务有数据';
        }
    } else if (last) {
        asr = last.length > 18 ? last.slice(0, 18) + '…' : last;
    }
    document.getElementById('g1-mic-asr').innerText = m.error ? ('错误：' + m.error) : asr;
    drawWave(m.envelope || []);

    // 录音是"最近一次"，停了也还在，标清楚免得误会在实时录
    document.getElementById('g1-mic-secs').innerText =
        (m.record_seconds || 0).toFixed(1) + ' s' + (active ? '' : '（上次）');

    // ---- 自动切句片段 ----
    const segs = d.segments || [];
    const list = document.getElementById('g1-segments');
    const pill = document.getElementById('g1-seg-pill');
    if (pill) {
        const label = pill.querySelector('span');
        if (label) label.innerText = `${(d.mic && d.mic.segments) || segs.length} 条`;
        pill.querySelector('.g1-dot').className = 'g1-dot' + (segs.length ? ' on' : '');
    }
    if (list) {
        const sig = segs.map(s => s.id).join(',');
        if (list.dataset.sig !== sig) {
            list.dataset.sig = sig;
            if (!segs.length) {
                list.innerHTML = '<div class="g1-sub">还没有片段。点左侧「开始自动监听」，然后正常说话即可。</div>';
            } else {
                list.innerHTML = segs.slice().reverse().map(s => `
                    <div class="g1-seg">
                        <span class="t">${s.time}</span>
                        <span class="d">${Number(s.dur).toFixed(2)}s</span>
                        <button onclick="playSeg('${s.id}')">播放</button>
                        <a href="/api/sensors/segments/${s.id}.wav" download>下载</a>
                    </div>`).join('');
            }
        }
    }

    // ---- IMU ----
    const imu = d.imu;
    if (imu) {
        const r = (imu.rpy[0] * 180 / Math.PI), p = (imu.rpy[1] * 180 / Math.PI), y = (imu.rpy[2] * 180 / Math.PI);
        document.getElementById('g1-rpy-r').innerText = r.toFixed(1) + '°';
        document.getElementById('g1-rpy-p').innerText = p.toFixed(1) + '°';
        document.getElementById('g1-rpy-y').innerText = y.toFixed(1) + '°';
        document.getElementById('g1-gyro').innerText = imu.gyro.map(x => x.toFixed(2)).join(', ');
        document.getElementById('g1-acc').innerText = imu.acc.map(x => x.toFixed(1)).join(', ');
        document.getElementById('g1-imu-temp').innerText = imu.temp + ' °C';
        const h = document.getElementById('g1-att-h');
        if (h) h.style.transform = `rotate(${r.toFixed(1)}deg) translateY(${Math.max(-40, Math.min(40, p * 1.1)).toFixed(1)}px)`;
        setPill('g1-imu-pill', '实时', 'on');
    } else {
        setPill('g1-imu-pill', '无数据', 'off');
    }

    // ---- 关节 ----
    const motors = d.motors || [];
    if (motors.length) {
        setPill('g1-joint-pill', `${motors.length} 路`, 'on');
        renderJoints(motors);
    } else {
        setPill('g1-joint-pill', '无数据', 'off');
    }

    // ---- 传感器清单 ----
    const chips = document.getElementById('g1-sensor-chips');
    if (chips && d.sensors) {
        chips.innerHTML = d.sensors.map(s => `
            <div class="g1-chip">
                <i class="g1-dot ${s.ok ? 'on' : ''}" style="margin-top:2px"></i>
                <div><div>${s.name}</div><small>${s.detail}</small></div>
            </div>`).join('');
    }
    const remote = document.getElementById('g1-remote');
    if (remote) remote.innerText = '遥控器 ' + (d.remote_hex || '—');
}

function renderJoints(motors) {
    const body = document.getElementById('g1-joint-body');
    if (!body) return;
    if (sensorJointsBuilt !== motors.length) {
        body.innerHTML = motors.map(m =>
            `<tr><td>${m.i}</td><td class="q"></td><td class="dq"></td><td class="tau"></td><td class="t"></td></tr>`
        ).join('');
        sensorJointsBuilt = motors.length;
    }
    const rows = body.children;
    for (let i = 0; i < motors.length && i < rows.length; i++) {
        const m = motors[i], tr = rows[i];
        tr.children[1].innerText = m.q.toFixed(3);
        tr.children[2].innerText = m.dq.toFixed(2);
        tr.children[3].innerText = m.tau.toFixed(1);
        tr.children[4].innerText = m.t;
    }
}

function drawWave(env) {
    const cv = document.getElementById('g1-mic-wave');
    if (!cv || !cv.getContext) return;
    const ctx = cv.getContext('2d');
    const w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#f8fafc';
    ctx.fillRect(0, 0, w, h);
    const n = env.length;
    if (!n) {
        ctx.strokeStyle = '#e2e8f0';
        ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
        return;
    }
    const step = w / Math.max(60, n);
    for (let i = 0; i < n; i++) {
        const v = Math.min(1, env[i] * 6);
        const bh = Math.max(1.5, v * (h - 8));
        ctx.fillStyle = v > 0.75 ? '#f59e0b' : '#10b981';
        ctx.fillRect(i * step + 1, (h - bh) / 2, Math.max(2, step - 2), bh);
    }
}

function micStart() {
    fetch('/api/sensors/mic/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto: true })
    })
        .then(r => r.json())
        .then(d => { showToast(d.msg || '已发送'); setTimeout(pollSensors, 800); })
        .catch(() => showToast('麦克风启动失败', 'error'));
}

function micStop() {
    fetch('/api/sensors/mic/stop', { method: 'POST' })
        .then(r => r.json())
        .then(d => { showToast(d.msg || '已发送'); pollSensors(); })
        .catch(() => showToast('停止失败', 'error'));
}

function downloadWav() {
    window.location.href = '/api/sensors/mic.wav';
}

function playSeg(id) {
    const a = document.getElementById('g1-audio');
    if (!a) return;
    a.src = `/api/sensors/segments/${id}.wav?ts=${Date.now()}`;
    a.play().catch(() => showToast('播放失败', 'error'));
}

function clearSegments() {
    if (!confirm('清空已切出来的片段（文件也会删掉）？')) return;
    fetch('/api/sensors/segments/clear', { method: 'POST' })
        .then(r => r.json())
        .then(d => { showToast(d.msg || '已清空'); pollSensors(); })
        .catch(() => showToast('清空失败', 'error'));
}

function robotSay(text) {
    const box = document.getElementById('g1-tts-text');
    const content = (text || (box && box.value) || '').trim();
    if (!content) {
        showToast('先说点什么', 'error');
        return;
    }
    fetch('/api/sensors/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: content })
    })
        .then(r => r.json())
        .then(d => showToast(d.msg || (d.ok ? '已播报' : '播报失败'), d.ok ? 'success' : 'error'))
        .catch(() => showToast('播报请求失败', 'error'));
}

function startSensorBridge() {
    showToast('正在拉起传感器桥…');
    fetch('/api/sensors/start', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            showToast(d.msg || (d.ok ? '已启动' : '启动失败'));
            setTimeout(() => { startSensorPolling(); checkHealth(); }, 2200);
        })
        .catch(() => showToast('启动失败：主控台无响应', 'error'));
}

window.onload = function() {
    checkHealth();
    loadRedeemCodes();
    setInterval(checkHealth, 4000);
    // 支持 #sensors 这种直达链接（截图/排障时好用）
    rcFillPoses(null);
    const hash = (location.hash || '').replace('#', '');
    if (hash && document.getElementById('tab-' + hash)) switchTab(hash);
};

/* ==================================================================
   动作模组页
   ------------------------------------------------------------------
   页面只跟主控台说话（/api/motion/*），主控台再转发到 127.0.0.1:5012
   的动作桥。动作桥用 conda g1 解释器跑，负责真正的 DDS 回放。
   ================================================================== */
let motionTimer = null;
let motionSelected = null;
let motionLibrary = [];

function startMotionPolling() {
    if (motionTimer) return;
    pollMotion();
    motionTimer = setInterval(pollMotion, 800);
}

function stopMotionPolling() {
    if (motionTimer) { clearInterval(motionTimer); motionTimer = null; }
}

function startMotionBridge() {
    showToast('正在拉起动作模组桥…');
    fetch('/api/motion/start', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            showToast(d.msg || (d.ok ? '已启动' : '启动失败'), d.ok ? 'success' : 'error');
            setTimeout(() => startMotionPolling(), 2200);
        })
        .catch(() => showToast('启动失败：主控台无响应', 'error'));
}

function pollMotion() {
    if (currentTab !== 'motion') return;
    fetch('/api/motion/state')
        .then(r => r.json())
        .then(d => {
            if (!d.ok) return motionDown(d.msg || '动作桥未启动');
            renderMotionState(d);
        })
        .catch(() => motionDown('动作桥未启动'));
}

function motionDown(msg) {
    setPill('motion-link-pill', '桥未启动', 'off');
    setPill('motion-state-pill', '不可用', 'off');
    const sel = document.getElementById('motion-selected');
    if (sel) sel.innerText = msg + ' · 点右上角「启动动作桥」';
}

function renderMotionState(d) {
    if (d.ready) {
        const modeTxt = d.mode ? ` · 模式 ${d.mode}` : '';
        setPill('motion-link-pill', `就绪 · DDS 已连接${modeTxt}`, 'on');
    } else {
        let reason = '无 lowstate';
        if (!(d.dds && d.dds.ok)) reason = 'DDS 未绑定';
        else if (!d.reachable) reason = 'ping 失败';
        setPill('motion-link-pill', `桥已启动，机器人未就绪 · ${reason}`, 'warn');
    }
    // 机器人链路
    if (!d.reachable) {
        setPill('motion-state-pill', '机器人不可达', 'warn');
    }
    // 动作库
    motionLibrary = d.library || [];
    document.getElementById('motion-count').innerText = motionLibrary.length + ' 个';
    renderMotionList();
    // 首次进页面自动选中第一个动作，曲线不至于空着
    if (!motionSelected && motionLibrary.length) {
        motionSelect(motionLibrary[0].name);
    }
    // 会话状态
    const s = d.session;
    const pct = s && s.frames ? Math.round((s.progress || 0) * 100) : 0;
    const bar = document.getElementById('motion-bar');
    if (bar) bar.style.width = pct + '%';
    const pctEl = document.getElementById('motion-progress-pct');
    if (pctEl) pctEl.innerText = pct + '%';
    const txt = document.getElementById('motion-progress-text');
    if (txt) {
        if (!s) txt.innerText = '—';
        else txt.innerText = (s.msg || s.state) + (s.frames ? ` · ${s.index}/${s.frames} 帧` : '');
    }
    if (s) {
        const map = { pending: ['排队中', 'warn'], releasing: ['释放模式中', 'warn'],
                      running: ['回放中', 'on'], done: ['已完成', 'on'],
                      stopped: ['已停止', 'off'], error: ['出错', 'off'] };
        const [label, st] = map[s.state] || [s.state, 'off'];
        setPill('motion-state-pill', label, st);
        if (s.state === 'error' && s.error) showToast(s.error, 'error');
    } else if (d.reachable) {
        setPill('motion-state-pill', '空闲', 'off');
    }
    const btn = document.getElementById('motion-play-btn');
    if (btn) btn.disabled = !!(s && ['pending', 'releasing', 'running'].includes(s.state));
}

function renderMotionList() {
    const box = document.getElementById('motion-list');
    if (!box) return;
    if (!motionLibrary.length) {
        box.innerHTML = '<div class="g1-sub">还没有动作，先导入一个 .g1motion 文件</div>';
        return;
    }
    box.innerHTML = motionLibrary.map(m => {
        const active = motionSelected === m.name ? ' active' : '';
        const dur = m.duration != null ? m.duration + 's' : '—';
        const frames = m.frames != null ? m.frames + ' 帧' : '—';
        const fps = m.fps != null ? m.fps + 'Hz' : '—';
        return `<div class="motion-row${active}" onclick="motionSelect('${m.name}')">
            <span class="nm">${m.name}</span>
            <span class="meta">${frames} · ${fps} · ${dur} · ${m.size_kb}KB</span>
            <span class="act">
                <button onclick="event.stopPropagation();motionSelect('${m.name}')">曲线</button>
                <button onclick="event.stopPropagation();motionSelect('${m.name}');motionPlay()">播放</button>
                <button class="del" onclick="event.stopPropagation();motionDelete('${m.name}')">删除</button>
            </span>
        </div>`;
    }).join('');
}

function motionSelect(name) {
    motionSelected = name;
    document.getElementById('motion-selected').innerText = '已选：' + name;
    renderMotionList();
    fetch('/api/motion/curve?name=' + encodeURIComponent(name))
        .then(r => r.json())
        .then(d => {
            if (!d.ok) return showToast(d.msg || '取曲线失败', 'error');
            drawMotionCurve(d);
        })
        .catch(() => showToast('取曲线失败', 'error'));
}

function drawMotionCurve(d) {
    const cv = document.getElementById('motion-curve');
    if (!cv) return;
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H);
    const labels = Object.keys(d.curves || {});
    if (!labels.length) return;
    let min = Infinity, max = -Infinity;
    labels.forEach(k => d.curves[k].forEach(v => { if (v < min) min = v; if (v > max) max = v; }));
    if (!isFinite(min) || max - min < 1e-6) { min -= 1; max += 1; }
    const padX = 30, padTop = 46, padBottom = 20;
    const y = v => H - padBottom - (v - min) / (max - min) * (H - padTop - padBottom);
    // 零线
    ctx.strokeStyle = '#eef2f7'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padX, y(0)); ctx.lineTo(W - padX, y(0)); ctx.stroke();
    const colors = ['#4f46e5', '#0ea5e9', '#f97316', '#ef4444', '#10b981'];
    labels.forEach((k, i) => {
        const arr = d.curves[k];
        ctx.strokeStyle = colors[i % colors.length];
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        arr.forEach((v, j) => {
            const x = padX + j / Math.max(1, arr.length - 1) * (W - padX * 2);
            if (j === 0) ctx.moveTo(x, y(v)); else ctx.lineTo(x, y(v));
        });
        ctx.stroke();
        ctx.fillStyle = colors[i % colors.length];
        ctx.font = '11px sans-serif';
        ctx.fillText(k, padX + i * 78, 22);
    });
    document.getElementById('motion-curve-info').innerText =
        `${d.frames} 帧 · ${d.fps}Hz · 抽稀 1/${d.step}`;
}

function motionPickFile(input) {
    const f = input.files && input.files[0];
    if (f) motionUpload(f);
    input.value = '';
}

function motionUpload(file) {
    const okExt = /\.(g1motion|npy|csv)$/i.test(file.name);
    if (!okExt) return showToast('只支持 .g1motion / .npy / .csv', 'error');
    showToast('正在导入 ' + file.name + ' …');
    fetch('/api/motion/upload?name=' + encodeURIComponent(file.name), {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file
    })
        .then(r => r.json())
        .then(d => {
            showToast(d.msg || (d.ok ? '已导入' : '导入失败'), d.ok ? 'success' : 'error');
            if (d.ok) { motionSelected = d.name; pollMotion(); setTimeout(() => motionSelect(d.name), 400); }
        })
        .catch(() => showToast('上传失败：主控台无响应', 'error'));
}

function motionPlay() {
    if (!motionSelected) return showToast('先在动作库里选一个动作', 'error');
    const speed = parseFloat(document.getElementById('motion-speed').value || '0.5');
    const mode = document.getElementById('motion-mode').value;
    const tip = mode === 'lowlevel'
        ? '低层模式会接管 29 个关节，没有吊架机器人会摔。确定继续？'
        : mode === 'policy'
            ? '策略模式：机器人会自己保持平衡，但这是首次真机验证前的实验模式。\n务必吊架 + 有人扶。确定继续？'
            : `将以 ${speed}x 回放 ${motionSelected}（上半身，内部平衡保持）。确定？`;
    if (!confirm(tip)) return;
    fetch('/api/motion/play', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: motionSelected, speed: speed, mode: mode })
    })
        .then(r => r.json())
        .then(d => showToast(d.msg || (d.ok ? '已开始回放' : '回放失败'), d.ok ? 'success' : 'error'))
        .catch(() => showToast('回放请求失败', 'error'));
}

function motionStop() {
    fetch('/api/motion/stop', { method: 'POST' })
        .then(r => r.json())
        .then(d => showToast(d.msg || (d.ok ? '已停止' : '当前没有播放'), d.ok ? 'success' : 'error'))
        .catch(() => showToast('停止请求失败', 'error'));
}

function motionDelete(name) {
    if (!confirm('删除动作模组 ' + name + ' ？')) return;
    fetch('/api/motion/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name })
    })
        .then(r => r.json())
        .then(d => {
            showToast(d.msg || (d.ok ? '已删除' : '删除失败'), d.ok ? 'success' : 'error');
            if (d.ok) { if (motionSelected === name) motionSelected = null; pollMotion(); }
        })
        .catch(() => showToast('删除请求失败', 'error'));
}

// 拖拽导入
window.addEventListener('DOMContentLoaded', () => {
    const drop = document.getElementById('motion-drop');
    if (!drop) return;
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
        e.preventDefault(); e.stopPropagation(); drop.classList.add('over');
    }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
        e.preventDefault(); e.stopPropagation(); drop.classList.remove('over');
    }));
    drop.addEventListener('drop', e => {
        const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (f) motionUpload(f);
    });
});

/* ==================================================================
   接待工作台：听 → 生成 → 呈现/出票
   ------------------------------------------------------------------
   生成走本机 Ollama（qwen3:8b），由主控台转发；出票记录进 data/tickets.csv，
   礼品码从 codes.csv 的 unused 池里取，核销中心可直接核销。
   ================================================================== */
let rcTicket = null;
let rcLlmTimer = null;
let rcBusy = false;
let rcMicTimer = null;
let rcAutoTimer = null;
let rcLastAutoId = null;
let rcVisTimer = null;
let rcVisStreaming = false;

const RC_PATTERN_GLYPH = { star: '✦', heart: '♥', wave: '〜', emblem: '◈', spark: '✧', circle: '◯' };

function startReceptionPolling() {
    refreshLlm();
    loadReceptionToday();
    pollReceptionMic();
    rcPollAuto();
    rcLoadPoses();
    rcPollVision();
    if (!rcMicTimer) rcMicTimer = setInterval(pollReceptionMic, 1200);
    if (!rcAutoTimer) rcAutoTimer = setInterval(rcPollAuto, 1500);
    if (!rcVisTimer) rcVisTimer = setInterval(rcPollVision, 1500);
    if (rcLlmTimer) clearInterval(rcLlmTimer);
    rcLlmTimer = setInterval(() => { refreshLlm(true); }, 15000);
}

/* 姿势下拉直接用动作库；进页面时拉一次（不用先去「动作与剪影」） */
function rcLoadPoses() {
    fetch('/api/motion/state')
        .then(r => r.json())
        .then(d => {
            if (d && Array.isArray(d.library)) {
                motionLibrary = d.library;
                const sel = document.getElementById('rc-pose');
                rcFillPoses(sel ? sel.value : null);
            }
        })
        .catch(() => { });
}

function stopReceptionPolling() {
    if (rcLlmTimer) { clearInterval(rcLlmTimer); rcLlmTimer = null; }
    if (rcMicTimer) { clearInterval(rcMicTimer); rcMicTimer = null; }
    if (rcAutoTimer) { clearInterval(rcAutoTimer); rcAutoTimer = null; }
    if (rcVisTimer) { clearInterval(rcVisTimer); rcVisTimer = null; }
}

/* ---- 自动接待：开一次，后面收音→识别→生成→播报→摆姿势全自动 ---- */
function rcToggleAuto(on) {
    fetch('/api/reception/auto', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: !!on })
    }).then(r => r.json()).then(d => {
        if (!d.ok) {
            showToast(d.msg || '切换失败', 'error');
            const cb = document.getElementById('rc-auto-toggle');
            if (cb) cb.checked = !on;
            return;
        }
        showToast(d.msg);
        rcPollAuto();
    }).catch(() => {
        showToast('切换失败：主控台无响应', 'error');
        const cb = document.getElementById('rc-auto-toggle');
        if (cb) cb.checked = !on;
    });
}

function rcPollVision() {
    fetch('/api/vision/state')
        .then(r => r.json())
        .then(d => {
            const v = (d && d.data) || {};
            const img = document.getElementById('rc-vis-img');
            const empty = document.getElementById('rc-vis-empty');
            const pill = document.getElementById('rc-vis-pill');
            const setText = (id, t) => { const el = document.getElementById(id); if (el) el.innerText = t; };
            const ok = !!v.bridge;
            if (pill) {
                const dot = pill.querySelector('.g1-dot');
                if (dot) dot.className = 'g1-dot' + (v.present ? ' on' : (ok ? ' warn' : ' off'));
                const label = pill.querySelector('span');
                if (label) label.innerText = !ok ? '未启动' : (v.present ? '有人在场' : '无人');
            }
            const frameAge = v.last_frame_age_ms;
            const hasFrame = ok && frameAge !== null && frameAge !== undefined && frameAge < 4000;
            if (empty) {
                empty.innerText = !ok ? '视觉桥未启动'
                    : (hasFrame ? '' : (v.error || '相机没有画面（检查头部相机排线/供电）'));
            }
            if (img) {
                if (hasFrame) {
                    img.style.display = 'block';
                    if (empty) empty.style.display = 'none';
                    if (!rcVisStreaming) {
                        rcVisStreaming = true;
                        img.src = '/api/vision/stream.mjpg?ts=' + Date.now();
                    }
                } else {
                    img.style.display = 'none';
                    img.removeAttribute('src');
                    rcVisStreaming = false;
                    if (empty) empty.style.display = 'flex';
                }
            }
            setText('rc-vis-present', ok ? (v.present ? `在场 ${v.present_for || 0}s` : '无人') : '—');
            setText('rc-vis-count', ok ? `${(v.faces || []).length} / ${(v.persons || []).length}` : '—');
            const lidar = v.lidar || {};
            setText('rc-vis-lidar', lidar.online ? '在线（驱动未接入）' : '未上线');
            const cb = document.getElementById('rc-vis-auto');
            if (cb && cb.checked !== !!v.autostart) cb.checked = !!v.autostart;
        })
        .catch(() => { });
}

function rcVisionCfg() {
    const cb = document.getElementById('rc-vis-auto');
    fetch('/api/vision/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ autostart: !!(cb && cb.checked) })
    }).then(r => r.json()).then(d => showToast(d.msg || '已保存'))
        .catch(() => showToast('保存失败', 'error'));
}

function rcPollAuto() {
    fetch('/api/reception/auto')
        .then(r => r.json())
        .then(d => {
            if (!d.ok) return;
            const a = d.data || {};
            const banner = document.getElementById('rc-auto-banner');
            if (banner) banner.classList.toggle('hidden', !a.on);
            const cb = document.getElementById('rc-auto-toggle');
            if (cb && cb.checked !== !!a.on) cb.checked = !!a.on;
            const note = document.getElementById('rc-auto-note');
            if (note) {
                let msg = a.note || (a.on ? '等待观众说话…' : '');
                if (a.on && a.asr_ready === false) {
                    msg += ' · 离线识别未就绪（' + (a.asr_error || 'vosk 未安装') + '）';
                }
                note.innerText = msg;
            }
            const cnt = document.getElementById('rc-auto-count');
            if (cnt) cnt.innerText = `${a.count || 0} 件`;
            if (a.last && a.last.ticket && a.last.ticket.id !== rcLastAutoId) {
                rcLastAutoId = a.last.ticket.id;
                rcTicket = a.last.ticket;
                rcRenderTicket({ ticket: a.last.ticket, reply: a.last.reply });
                const txt = document.getElementById('rc-text');
                if (txt && a.last.text) txt.value = a.last.text;
                const pill = document.getElementById('rc-gen-pill');
                if (pill) {
                    pill.querySelector('.g1-dot').className = 'g1-dot on';
                    pill.querySelector('span').innerText = '已生成（自动）';
                }
                loadReceptionToday();
                showToast(`自动完成 ${a.last.ticket.id}`);
            }
        })
        .catch(() => { });
}

function stopLlm() {
    if (!confirm('关闭本机 Ollama？下次要用再点「启动模型」。')) return;
    fetch('/api/llm/stop', { method: 'POST' })
        .then(r => r.json())
        .then(d => { showToast(d.msg || (d.ok ? '已关闭' : '关闭失败'), d.ok ? 'success' : 'error'); refreshLlm(true); })
        .catch(() => showToast('关闭失败：主控台无响应', 'error'));
}

function pollReceptionMic() {
    if (currentTab !== 'control') return;
    fetch('/api/sensors/state')
        .then(r => r.json())
        .then(d => {
            if (!d.ok) { setPill('rc-mic-pill', '传感桥未启动', 'off'); return; }
            const mic = d.mic || {};
            const rms = Number(mic.rms || 0);
            const db = rms > 1e-6 ? 20 * Math.log10(rms) : -90;
            const bar = document.getElementById('rc-mic-bar');
            const dbEl = document.getElementById('rc-mic-db');
            const on = !!(mic.active || mic.recording);
            if (bar) bar.style.width = on ? Math.max(2, Math.min(100, (db + 60) / 60 * 100)) + '%' : '0%';
            if (dbEl) dbEl.innerText = on ? `${db.toFixed(1)} dB` : '-∞ dB';
            setPill('rc-mic-pill', on ? `监听中 · ${mic.segments || 0} 句` : '未监听', on ? 'on' : 'off');
        })
        .catch(() => setPill('rc-mic-pill', '传感桥未启动', 'off'));
}

function startLlm() {
    showToast('正在拉起本机模型…');
    fetch('/api/llm/start', { method: 'POST' })
        .then(r => r.json())
        .then(d => { showToast(d.msg || (d.ok ? '已启动' : '启动失败'), d.ok ? 'success' : 'error'); refreshLlm(true); })
        .catch(() => showToast('启动失败：主控台无响应', 'error'));
}

function refreshLlm(quiet) {
    fetch('/api/llm/status')
        .then(r => r.json())
        .then(d => {
            const el = document.getElementById('llm-pill');
            if (!el) return;
            const dot = el.querySelector('.g1-dot');
            const label = el.querySelector('span');
            const ok = d.ok;
            if (dot) dot.className = 'g1-dot' + (ok ? ' on' : ' off');
            if (label) label.innerText = ok ? `${d.data.model} 就绪` : '模型未就绪';
            const sub = document.getElementById('rc-gen-sub');
            if (sub && d.data) {
                sub.innerText = ok
                    ? `本机 ${d.data.model} · ${d.data.provider} · ${d.data.latency_ms}ms`
                    : (d.msg || '模型未就绪');
            }
            if (!quiet && !ok) showToast(d.msg || '模型没连上', 'error');
            if (!quiet && ok) showToast('模型已就绪：' + d.data.model);
        })
        .catch(() => { });
}

function rcPoses() {
    // 姿势只来自动作库：.g1motion 与 36 列 rl_sar CSV 都能回放
    return (motionLibrary || []).map(m => ({
        id: m.name,
        name: m.name.replace(/\.(g1motion|csv|npy)$/i, '')
    }));
}

function rcFillPoses(selected) {
    const sel = document.getElementById('rc-pose');
    if (!sel) return;
    const list = rcPoses();
    if (!list.length) {
        sel.innerHTML = '<option value="">（动作库为空，先去「动作与剪影」导入）</option>';
        return;
    }
    sel.innerHTML = list.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
    if (selected) {
        const hit = list.find(p => p.id === selected);
        if (hit) sel.value = selected;
    }
}

function rcGenerate(regen) {
    if (rcBusy) return;
    const text = (document.getElementById('rc-text').value || '').trim();
    if (!text) { showToast('先说一句话', 'error'); return; }
    rcBusy = true;
    const pill = document.getElementById('rc-gen-pill');
    if (pill) {
        pill.querySelector('.g1-dot').className = 'g1-dot warn';
        pill.querySelector('span').innerText = '生成中…';
    }
    fetch('/api/reception/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, poses: rcPoses() })
    })
        .then(r => r.json())
        .then(d => {
            rcBusy = false;
            if (!d.ok) {
                showToast(d.msg || '生成失败', 'error');
                if (pill) { pill.querySelector('.g1-dot').className = 'g1-dot off'; pill.querySelector('span').innerText = '生成失败'; }
                return;
            }
            rcTicket = d.data.ticket;
            rcRenderTicket(d.data);
            if (pill) {
                pill.querySelector('.g1-dot').className = 'g1-dot on';
                pill.querySelector('span').innerText = d.data.fallback ? '兜底生成' : '已生成';
            }
            const info = document.getElementById('rc-gen-info');
            if (info) {
                info.innerText = d.data.fallback
                    ? '本机模型没响应，用了兜底模板（记忆点=原话前 15 字）'
                    : `本机模型生成 · ${d.data.latency_ms} ms`;
            }
            showToast(regen ? '换了一个' : '作品生成好了');
        })
        .catch(() => {
            rcBusy = false;
            showToast('生成请求失败：主控台无响应', 'error');
        });
}

function rcRegenerate() { rcGenerate(true); }

function rcRenderTicket(d) {
    const t = d.ticket;
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
    setText('rcpt-id', t.id);
    setText('rcpt-time', (t.created || '').slice(0, 16));
    setText('rcpt-memory', t.memory);
    setText('rcpt-code', t.code);
    setText('rcpt-pattern', RC_PATTERN_GLYPH[t.pattern] || '✦');
    const mem = document.getElementById('rc-memory');
    if (mem) mem.value = t.memory;
    const rep = document.getElementById('rc-reply');
    if (rep) rep.value = d.reply || '';
    const pat = document.getElementById('rc-pattern');
    if (pat) pat.value = t.pattern;
    rcFillPoses(t.pose);
}

function rcCurrentTicket() {
    if (!rcTicket) return null;
    // 以输入框里的最新内容为准
    const mem = document.getElementById('rc-memory');
    const pat = document.getElementById('rc-pattern');
    const pose = document.getElementById('rc-pose');
    return {
        ...rcTicket,
        memory: mem ? mem.value.trim() || rcTicket.memory : rcTicket.memory,
        pattern: pat ? pat.value : rcTicket.pattern,
        pose: pose ? pose.value : rcTicket.pose,
    };
}

function rcSpeak() {
    const t = rcCurrentTicket();
    if (!t) return showToast('先点「生成作品」', 'error');
    fetch('/api/sensors/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: t.memory })
    })
        .then(r => r.json())
        .then(d => showToast(d.msg || (d.ok ? '机器人已播报' : '播报失败'), d.ok ? 'success' : 'error'))
        .catch(() => showToast('播报请求失败', 'error'));
}

function rcPose() {
    const t = rcCurrentTicket();
    if (!t) return showToast('先点「生成作品」', 'error');
    if (!t.pose) return showToast('动作库是空的，先去「动作与剪影」导入一个动作', 'error');
    const speed = parseFloat((document.getElementById('motion-speed') || {}).value || '0.2');
    fetch('/api/motion/play', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: t.pose, speed: speed, mode: 'arm_sdk' })
    })
        .then(r => r.json())
        .then(d => showToast(d.msg || (d.ok ? '机器人开始摆姿势' : '失败'), d.ok ? 'success' : 'error'))
        .catch(() => showToast('回放请求失败', 'error'));
}

function rcCapture() {
    const t = rcCurrentTicket();
    if (!t) return showToast('先点「生成作品」', 'error');
    const url = '/api/sensors/video.jpg?ts=' + Date.now();
    fetch(url)
        .then(r => { if (!r.ok) throw new Error('no frame'); return r.blob(); })
        .then(blob => {
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `剪影_${t.id.replace('#', '')}.jpg`;
            a.click();
            setTimeout(() => URL.revokeObjectURL(a.href), 3000);
            showToast('已抓取当前画面（剪影轮廓需在幕布场景下才明显）');
        })
        .catch(() => showToast('取画面失败：传感器桥未启动或没有视频源', 'error'));
}

function rcSaveTicket(t) {
    return fetch('/api/reception/ticket', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticket: t })
    }).then(r => r.json());
}

function rcPrint() {
    const t = rcCurrentTicket();
    if (!t) return showToast('先点「生成作品」', 'error');
    rcSaveTicket(t).then(() => loadReceptionToday()).catch(() => { });

    const glyph = RC_PATTERN_GLYPH[t.pattern] || '✦';
    const html = `<!doctype html><html><head><meta charset="utf-8"><title>创作凭证 ${t.id}</title>
<style>
@page { size: 58mm auto; margin: 3mm; }
body { font-family: "Microsoft YaHei", sans-serif; width: 52mm; margin: 0 auto; color:#000; }
.c { text-align:center; }
.t { font-size: 12px; font-weight: 700; letter-spacing: .5px; }
.s { font-size: 9px; color:#333; margin-top:2px; }
hr { border:0; border-top:1px dashed #000; margin:5px 0; }
.row { display:flex; justify-content:space-between; font-size:9.5px; margin:2px 0; }
.mem { font-size: 12px; font-weight:700; text-align:center; line-height:1.5; margin:6px 0; }
.pat { font-size: 26px; text-align:center; margin:2px 0 6px; }
.lab { font-size: 8.5px; text-align:center; }
.code { font-size: 18px; font-weight:800; letter-spacing:3px; text-align:center; margin:2px 0 6px; }
.foot { font-size: 8.5px; text-align:center; line-height:1.5; }
</style></head><body>
<div class="c t">G1 校园数字共创</div>
<div class="c s">· 专属创作凭证 ·</div>
<hr>
<div class="row"><span>作品编号</span><span>${t.id}</span></div>
<div class="row"><span>完成时间</span><span>${(t.created || '').slice(0, 16)}</span></div>
<hr>
<div class="mem">${t.memory}</div>
<div class="pat">${glyph}</div>
<hr>
<div class="lab">随机礼品数字码</div>
<div class="code">${t.code}</div>
<div class="foot">凭此码到兑奖台兑换<br>3D 打印纪念品一枚</div>
</body></html>`;
    const w = window.open('', '_blank', 'width=360,height=640');
    if (!w) return showToast('浏览器拦截了打印窗口，请允许弹窗', 'error');
    w.document.write(html);
    w.document.close();
    setTimeout(() => { try { w.focus(); w.print(); } catch (e) { } }, 400);
    showToast('已送去打印');
}

function loadReceptionToday() {
    fetch('/api/reception/today')
        .then(r => r.json())
        .then(d => {
            const list = document.getElementById('rc-today-list');
            const cnt = document.getElementById('rc-today-count');
            if (!d.ok || !list) return;
            const rows = d.data.tickets || [];
            if (cnt) cnt.innerText = rows.length + ' 张';
            if (!rows.length) {
                list.innerHTML = '<div class="g1-sub">还没有出票</div>';
                return;
            }
            list.innerHTML = rows.slice().reverse().map(t => `
                <div class="row" style="cursor:pointer" title="点击去核销这个礼品码"
                     onclick="gotoRedeem('${t.code}')">
                    <span>${t.id} · ${t.memory}</span>
                    <b>${t.code}</b>
                </div>`).join('');
        })
        .catch(() => { });
}

function gotoRedeem(code) {
    switchTab('redeem');
    setTimeout(() => {
        const inp = document.getElementById('redeem-input');
        if (inp) { inp.value = code; inp.focus(); inp.select(); }
        showToast('已填入礼品码 ' + code + '，点「立即核销」完成兑换');
    }, 200);
}


/* ==================================================================
   虚拟形象与开机画面控制
   ================================================================== */
let _avatarState = { config: {}, assets: {} };
let _bootTimer = null;
let _bootProgressTimer = null;

function initAvatarSystem() {
    fetch('/api/avatar/info')
        .then(r => r.json())
        .then(res => {
            if (!res.ok || !res.data) return;
            _avatarState = res.data;
            applyAvatarAssets();
            const cfg = _avatarState.config || {};
            const stage = document.getElementById('mascot-stage');
            if (stage) stage.style.display = cfg.enabled === false ? 'none' : 'flex';
            if (cfg.enabled !== false && cfg.boot_enabled !== false && !sessionStorage.getItem('shiqian_boot_shown')) {
                sessionStorage.setItem('shiqian_boot_shown', '1');
                playBootSplash();
            }
        })
        .catch(() => {});
}

function applyAvatarAssets() {
    const a = (_avatarState && _avatarState.assets) || {};
    const cfg = (_avatarState && _avatarState.config) || {};
    const icon = document.getElementById('sidebar-avatar-img');
    if (icon && a.icon_url) icon.src = a.icon_url;
    const hero = document.getElementById('mascot-hero-img');
    if (hero && a.hero_url) hero.src = a.hero_url;
    const fav = document.getElementById('app-favicon');
    if (fav && a.icon_url) fav.href = a.icon_url;
    const nm = document.getElementById('mascot-name');
    if (nm && cfg.name) nm.innerText = `${cfg.name} · 虚拟看板`;
}

function playBootSplash() {
    const overlay = document.getElementById('boot-splash');
    const video = document.getElementById('boot-video');
    const bar = document.getElementById('boot-bar-fill');
    const pctEl = document.getElementById('boot-pct-text');
    const statusEl = document.getElementById('boot-status-text');
    const a = (_avatarState && _avatarState.assets) || {};
    const cfg = (_avatarState && _avatarState.config) || {};
    if (!overlay || !video || !a.boot_url) return;

    const duration = Math.max(1500, parseInt(cfg.boot_duration_ms) || 4200);
    video.src = a.boot_url;
    video.currentTime = 0;
    overlay.classList.remove('boot-hidden');
    overlay.setAttribute('aria-hidden', 'false');
    video.play().catch(() => {});

    const t0 = Date.now();
    if (_bootProgressTimer) clearInterval(_bootProgressTimer);
    _bootProgressTimer = setInterval(() => {
        const p = Math.min(100, Math.round(((Date.now() - t0) / duration) * 100));
        if (bar) bar.style.width = p + '%';
        if (pctEl) pctEl.innerText = p + '%';
        if (statusEl) {
            if (p < 40) statusEl.innerText = '正在唤醒时迁主控台...';
            else if (p < 85) statusEl.innerText = '正在检查本地桥接与机器人状态...';
            else statusEl.innerText = '准备就绪，欢迎回来！';
        }
    }, 80);

    if (_bootTimer) clearTimeout(_bootTimer);
    _bootTimer = setTimeout(dismissBootSplash, duration);
}

function replayBootSplash() {
    if (!(_avatarState && _avatarState.assets && _avatarState.assets.boot_url)) {
        fetch('/api/avatar/info').then(r => r.json()).then(res => {
            if (res.ok && res.data) {
                _avatarState = res.data;
                applyAvatarAssets();
                playBootSplash();
            }
        });
        return;
    }
    playBootSplash();
}

function dismissBootSplash() {
    const overlay = document.getElementById('boot-splash');
    const video = document.getElementById('boot-video');
    if (_bootTimer) { clearTimeout(_bootTimer); _bootTimer = null; }
    if (_bootProgressTimer) { clearInterval(_bootProgressTimer); _bootProgressTimer = null; }
    if (overlay) {
        overlay.classList.add('boot-hidden');
        overlay.setAttribute('aria-hidden', 'true');
    }
    if (video) {
        try { video.pause(); } catch (e) {}
    }
}

function updateMascotFromStatus(st) {
    const wrap = document.getElementById('mascot-avatar-wrap');
    const mood = document.getElementById('mascot-mood-pill');
    const bubble = document.getElementById('mascot-bubble');
    if (!wrap || !mood || !bubble || !st) return;

    wrap.classList.remove('mascot-ring-ready', 'mascot-ring-warn');
    if (st.sensor && st.sensor.ready) {
        wrap.classList.add('mascot-ring-ready');
        mood.innerText = '在线就绪';
        mood.style.background = '#ECF3EE';
        mood.style.color = '#254030';
        bubble.innerText = `「机器人在线就绪啦！当前传感器频率 ${st.sensor.hz || 0} Hz，随时可以开始交互或动作回放~」`;
    } else if ((st.sensor && st.sensor.bridge) || (st.motion && st.motion.bridge)) {
        wrap.classList.add('mascot-ring-warn');
        mood.innerText = '等待机器人';
        mood.style.background = '#FEF3C7';
        mood.style.color = '#92400E';
        const reason = (st.motion && st.motion.not_ready_reason) || (st.sensor && st.sensor.not_ready_reason) || '机器人未就绪';
        bubble.innerText = `「桥接进程已经起来了，不过还没完全连通机器人（${reason}），请检查网线与电源哦。」`;
    } else {
        mood.innerText = '待命中';
        mood.style.background = '#ECF3EE';
        mood.style.color = '#254030';
        bubble.innerText = '「你好呀，我是时迁！点击右上角一键启动系统，或去系统设置里检测 Bridge Python 吧。」';
    }
}

function uploadAvatarAsset(slot, input) {
    const file = input && input.files && input.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    showToast(`正在上传 ${slot} 素材...`);
    fetch(`/api/avatar/upload?slot=${encodeURIComponent(slot)}`, {
        method: 'POST',
        body: fd
    })
        .then(r => r.json())
        .then(res => {
            if (res.ok) {
                if (!_avatarState.assets) _avatarState.assets = {};
                _avatarState.assets = res.data || _avatarState.assets;
                applyAvatarAssets();
                showToast('形象素材已更新并生效！');
            } else {
                showToast(res.msg || '上传失败', 'error');
            }
        })
        .catch(() => showToast('上传请求失败', 'error'));
}

/* ==================================================================
   Promise 主控台 (:7100) 跨端桥接、顶部栏状态灯与快捷远程操控
   ================================================================== */
let _latestPromiseSnap = null;
let _latestTailscaleIps = [];

function resolveCrossDeviceUrl(rawUrl) {
    if (!rawUrl) return 'http://127.0.0.1:7100/dev';
    try {
        const u = new URL(rawUrl, window.location.origin);
        const viewerHost = window.location.hostname;
        const viewerIsRemote = viewerHost && viewerHost !== '127.0.0.1' && viewerHost !== 'localhost' && viewerHost !== '::1';
        if (viewerIsRemote && (u.hostname === '127.0.0.1' || u.hostname === 'localhost')) {
            u.hostname = viewerHost;
        }
        return u.toString();
    } catch (e) {
        return rawUrl;
    }
}

function updateAuthAndNetworkBadges(auth) {
    if (!auth) return;
    const port = window.location.port || '5000';
    const bindLabel = document.getElementById('prm-bind-label');
    if (bindLabel) {
        bindLabel.innerText = `${auth.bind_host || '127.0.0.1'}:${port}`;
    }

    const tsIps = auth.local_tailscale_ips || [];
    _latestTailscaleIps = tsIps;
    const tsChip = document.getElementById('prm-ts-chip');
    const tsText = document.getElementById('prm-ts-ip-text');
    if (tsChip && tsText) {
        if (tsIps.length > 0) {
            tsChip.style.display = 'inline-flex';
            tsText.innerText = `${tsIps[0]}:${port}`;
        } else {
            tsChip.style.display = 'none';
        }
    }

    const tsTargetWrap = document.getElementById('prm-ts-target-chips');
    if (tsTargetWrap) {
        tsTargetWrap.innerHTML = tsIps.map(ip =>
            `<button type="button" class="prm-chip-btn" onclick="switchPromiseTargetUrl('http://${ip}:7100')">Tailscale ${ip}:7100</button>`
        ).join('');
    }

    const hasSessionTok = !!(sessionStorage.getItem(_SHIQIAN_TOKEN_STORAGE_KEY) || '').trim();
    const badge = document.getElementById('prm-auth-badge');
    if (badge) {
        if (auth.token_configured) {
            badge.innerText = hasSessionTok ? `🔐 ${auth.header} 已就绪` : `🔒 需验证 ${auth.header}`;
            badge.style.background = hasSessionTok ? '#ecfdf5' : '#fef3c7';
            badge.style.color = hasSessionTok ? '#065f46' : '#92400e';
        } else if (auth.remote_client) {
            badge.innerText = `⚠️ 服务端未配 ${auth.token_env}`;
            badge.style.background = '#fee2e2';
            badge.style.color = '#b91c1c';
        } else {
            badge.innerText = `本机回环 · 远程需 ${auth.token_env}`;
            badge.style.background = '#f1f5f9';
            badge.style.color = '#475569';
        }
    }

    const envHint = document.getElementById('promise-token-env-hint');
    if (envHint) {
        envHint.innerText = auth.token_configured
            ? `✅ 已检测到环境变量 ${auth.token_env}（明文不落盘）`
            : `ℹ️ 当前未设置环境变量 ${auth.token_env}（本机可用，跨设备远程操控已自动锁定）`;
        envHint.className = 'text-xs font-medium ' + (auth.token_configured ? 'text-emerald-600' : 'text-slate-500');
    }
}

function refreshPromiseStatus(manual = false) {
    fetch('/api/promise/status')
        .then(r => r.json())
        .then(res => {
            if (!res || !res.data) return;
            _latestPromiseSnap = res.data;
            renderPromiseStatus(res.data);
            if (manual) {
                showToast(res.msg || (res.data.connected ? 'Promise 在线' : 'Promise 未连通'), res.data.connected ? 'success' : 'error');
            }
        })
        .catch(() => {});
}

function renderPromiseStatus(snap) {
    const pill = document.getElementById('promise-status-pill');
    const dot = document.getElementById('promise-status-dot');
    const label = document.getElementById('promise-status-label');
    const directLink = document.getElementById('promise-direct-link');
    const mobileLink = document.getElementById('promise-mobile-link');
    const targetInp = document.getElementById('prm-target-url');
    const teleLine = document.getElementById('prm-telemetry-line');

    if (directLink && snap.ui_url) directLink.href = resolveCrossDeviceUrl(snap.ui_url);
    if (mobileLink && snap.mobile_url) mobileLink.href = resolveCrossDeviceUrl(snap.mobile_url);
    if (targetInp && document.activeElement !== targetInp && snap.url) {
        targetInp.value = snap.url;
    }

    const p = snap.promise || {};
    const frozen = !!p.emergency_frozen;
    let hostShort = '127.0.0.1:7100';
    try { hostShort = new URL(snap.url).host; } catch (e) {}

    if (pill && dot && label) {
        if (!snap.enabled) {
            pill.className = 'prm-pill offline';
            dot.className = 'prm-dot offline';
            label.innerText = 'Promise 主控台 · 已禁用';
        } else if (snap.connected && frozen) {
            pill.className = 'prm-pill frozen';
            dot.className = 'prm-dot frozen';
            label.innerText = `Promise 紧急冻结 · ${hostShort}`;
        } else if (snap.connected) {
            pill.className = 'prm-pill online';
            dot.className = 'prm-dot online';
            const ms = snap.latency_ms !== null && snap.latency_ms !== undefined ? ` · ${snap.latency_ms}ms` : '';
            label.innerText = `Promise 在线 (${hostShort}${ms})`;
        } else {
            pill.className = 'prm-pill offline';
            dot.className = 'prm-dot offline';
            label.innerText = `Promise 离线 (${hostShort})`;
        }
    }

    if (teleLine) {
        if (snap.connected) {
            const ag = p.agent_states || {};
            const jy = ag.jinyan || 'idle';
            const xl = ag.xingluo || 'idle';
            const auditTxt = p.audit_chain_valid !== false ? '审计链完整' : '审计链异常';
            const freezeTxt = frozen ? ' · ⚠️ 紧急冻结中' : '';
            const batchTxt = (p.batches_count !== null && p.batches_count !== undefined) ? ` · 批次 ${p.batches_count}` : '';
            teleLine.innerText = `节点 ${snap.url} 在线（${snap.latency_ms || 0}ms）｜瑾妍: ${jy} · 星落: ${xl} · ${auditTxt}${batchTxt}${freezeTxt}`;
        } else {
            teleLine.innerText = `节点 ${snap.url || 'http://127.0.0.1:7100'} 未连接：${snap.error || '服务未启动'}（可先双击桌面 Promise_Workbench 启动）`;
        }
    }
}

function togglePromiseDrawer() {
    const drawer = document.getElementById('promise-drawer');
    const btn = document.getElementById('promise-drawer-btn');
    if (!drawer) return;
    const isOpen = drawer.classList.toggle('open');
    if (btn) btn.innerHTML = isOpen ? '<span>⚡ 收起操控 ▴</span>' : '<span>⚡ 远程操控 ▾</span>';
    if (isOpen) refreshPromiseStatus(false);
}

function saveSessionBridgeToken() {
    const inp = document.getElementById('prm-session-token');
    if (!inp) return;
    const val = inp.value.trim();
    if (!val) {
        showToast('请输入 X-Shiqian-Token 口令', 'error');
        return;
    }
    sessionStorage.setItem(_SHIQIAN_TOKEN_STORAGE_KEY, val);
    inp.value = '';
    inp.placeholder = '已载入当前会话口令（仅驻留内存，关闭标签页自动销毁）';
    showToast('X-Shiqian-Token 会话口令已生效');
    checkHealth();
}

function clearSessionBridgeToken() {
    sessionStorage.removeItem(_SHIQIAN_TOKEN_STORAGE_KEY);
    const inp = document.getElementById('prm-session-token');
    if (inp) {
        inp.value = '';
        inp.placeholder = '输入 X-Shiqian-Token 会话口令（仅存浏览器内存，不落盘）';
    }
    showToast('已清除当前浏览器会话口令');
    checkHealth();
}

function copyTailscaleConsoleUrl() {
    if (!_latestTailscaleIps.length) return;
    const port = window.location.port || '5000';
    const url = `http://${_latestTailscaleIps[0]}:${port}`;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(() => showToast('已复制跨端访问地址：' + url));
    } else {
        prompt('复制跨端访问地址：', url);
    }
}

function switchPromiseTargetUrl(presetUrl) {
    const inp = document.getElementById('prm-target-url');
    const targetUrl = (presetUrl || (inp && inp.value) || '').trim();
    if (!targetUrl) {
        showToast('请输入 Promise 主控台地址', 'error');
        return;
    }
    fetch('/api/promise/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_url', url: targetUrl })
    })
        .then(r => r.json())
        .then(res => {
            const resLine = document.getElementById('prm-last-result');
            if (resLine) resLine.innerText = res.msg || '';
            if (res.ok) {
                if (res.data) renderPromiseStatus(res.data);
                if (currentTab === 'settings') loadConfigToForm();
                showToast(res.msg || '目标地址已切换');
            } else {
                showToast(res.msg || '切换失败', 'error');
            }
        })
        .catch(() => showToast('切换目标请求失败', 'error'));
}

function fillPromisePreset(text) {
    const inp = document.getElementById('prm-cmd-input');
    if (inp) {
        inp.value = text;
        inp.focus();
    }
}

function checkPromiseNow() {
    const resLine = document.getElementById('prm-last-result');
    if (resLine) resLine.innerText = '正在向 Promise 发起实时状态检查...';
    fetch('/api/promise/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'status' })
    })
        .then(r => r.json())
        .then(res => {
            if (res.data) renderPromiseStatus(res.data);
            if (resLine) resLine.innerText = `[${new Date().toLocaleTimeString()}] ${res.msg || ''}`;
            showToast(res.msg || '状态检查完成', res.ok ? 'success' : 'error');
        })
        .catch(() => showToast('Promise 状态检查请求失败', 'error'));
}

function sendPromiseTask() {
    const inp = document.getElementById('prm-cmd-input');
    const targetSel = document.getElementById('prm-cmd-target');
    const resLine = document.getElementById('prm-last-result');
    const command = (inp && inp.value || '').trim();
    const target = (targetSel && targetSel.value) || 'agent.jinyan';
    if (!command) {
        showToast('请输入要发送给 Promise 的任务指令', 'error');
        if (inp) inp.focus();
        return;
    }
    if (resLine) resLine.innerText = `正在向 ${target} 发送指令...`;
    fetch('/api/promise/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'task', target, command })
    })
        .then(r => r.json())
        .then(res => {
            if (resLine) resLine.innerText = `[${new Date().toLocaleTimeString()}] ${res.msg || ''}`;
            if (res.ok) {
                if (inp) inp.value = '';
                showToast(res.msg || '任务指令已送达 Promise');
                refreshPromiseStatus(false);
            } else {
                showToast(res.msg || '指令发送失败', 'error');
            }
        })
        .catch(() => showToast('指令发送异常', 'error'));
}

function triggerPromiseEmergencyStop(alsoLocal = false) {
    const tip = alsoLocal
        ? '确定要执行【双端联动急停】吗？\n将同时紧急冻结 Promise 主控台 (:7100) 并停止本机时迁全部机器人桥接模块！'
        : '确定要向 Promise 主控台 (:7100) 发送【紧急停止】指令吗？';
    if (!confirm(tip)) return;
    const resLine = document.getElementById('prm-last-result');
    fetch('/api/promise/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'emergency_stop',
            also_stop_local: !!alsoLocal,
            reason: alsoLocal ? 'dual_emergency_stop_from_shiqian' : 'remote_emergency_stop_from_shiqian'
        })
    })
        .then(r => r.json())
        .then(res => {
            if (resLine) resLine.innerText = `[${new Date().toLocaleTimeString()}] ${res.msg || ''}`;
            showToast(res.msg || (res.ok ? '已执行紧急停止' : '紧急停止执行异常'), res.ok ? 'success' : 'error');
            checkHealth();
        })
        .catch(() => showToast('紧急停止请求异常', 'error'));
}

function initPromiseBridgeUI() {
    const inp = document.getElementById('prm-session-token');
    if (inp && (sessionStorage.getItem(_SHIQIAN_TOKEN_STORAGE_KEY) || '').trim()) {
        inp.placeholder = '已载入当前会话口令（仅驻留内存，关闭标签页自动销毁）';
    }
    refreshPromiseStatus(false);
}

window.addEventListener('DOMContentLoaded', () => {
    initAvatarSystem();
    initPromiseBridgeUI();
});
