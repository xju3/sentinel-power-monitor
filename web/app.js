const WS_PORT      = new URLSearchParams(window.location.search).get('wsPort') || '8765';
const WS_URL       = `ws://${window.location.hostname || 'localhost'}:${WS_PORT}`;
const SAMPLE_INTERVAL_MS = 200;
const MAX_POINTS   = 20000;

let data = [];
let windowCycles = 1;
let ws = null;
let lastChartUpdate = 0;
let latestStats = null;
let monitoringStarted = false;
const CHART_UPDATE_INTERVAL = 100; // 100ms 更流畅的更新频率

// ── Chart ─────────────────────────────────────────────────────────────────
// 自定义插件：处理滑动窗口下的周期分界线与独立的当前进度条
const progressLinePlugin = {
  id: 'progressLine',
  afterDraw: (chart) => {
    const dataset = chart.data.datasets[0].data;
    if (!dataset || !dataset.length) return;
    
    const ctx = chart.ctx;
    const chartArea = chart.chartArea;
    const meta = chart.getDatasetMeta(0);

    const cyclePointCount = latestStats?.cycle_point_count || 1;
    const absoluteSampleCount = latestStats?.absolute_sample_count || data.length;
    if (absoluteSampleCount === 0) return;

    // 统计当前图表内有效数据点
    let realDataCount = 0;
    let lastValidIdx = -1;
    for (let i = 0; i < dataset.length; i++) {
      if (dataset[i] !== null) {
        realDataCount++;
        lastValidIdx = i;
      }
    }

    ctx.save();

    // 1. 如果窗口未画满（第一周期初始），在波形头部画引导线
    if (realDataCount > 0 && realDataCount < dataset.length) {
      const pt = meta.data[lastValidIdx];
      if (pt) {
        ctx.beginPath();
        ctx.moveTo(pt.x, chartArea.top);
        ctx.lineTo(pt.x, chartArea.bottom);
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = '#e3b341';
        ctx.setLineDash([4, 4]);
        ctx.stroke();
      }
    }

    // 2. 绘制随波形向左滑动的“周期分界线” (C1, C2...)
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = 'rgba(227, 179, 65, 0.4)';
    ctx.setLineDash([4, 4]);
    for (let i = 0; i < realDataCount; i++) {
      const globalIdx = (absoluteSampleCount - realDataCount) + i;
      // 当全局索引是周期的倍数时，划定分割线
      if (globalIdx > 0 && globalIdx % cyclePointCount === 0) {
        const pt = meta.data[i];
        if (pt) {
          ctx.beginPath();
          ctx.moveTo(pt.x, chartArea.top);
          ctx.lineTo(pt.x, chartArea.bottom);
          ctx.stroke();
          // 标记周期编号
          const cNum = globalIdx / cyclePointCount;
          ctx.fillStyle = 'rgba(227, 179, 65, 0.7)';
          ctx.font = '10px "SF Mono", monospace';
          ctx.fillText(`C${cNum}`, pt.x + 4, chartArea.top + 10);
        }
      }
    }

    // 3. 绘制独立的底部横向进度条（永远从左向右扫，精准指示进度）
    const progress = (latestStats?.cycle_progress_points || 0) / cyclePointCount;
    const barWidth = chartArea.right - chartArea.left;
    const fillWidth = barWidth * progress;

    // 进度条暗色背景槽
    ctx.fillStyle = 'rgba(255, 255, 255, 0.05)';
    ctx.fillRect(chartArea.left, chartArea.bottom - 0, barWidth, 1);
    
    // 进度条高亮琥珀色
    ctx.fillStyle = '#e3b341';
    ctx.fillRect(chartArea.left, chartArea.bottom - 0, fillWidth, 1);

    ctx.restore();
  }
};

const chartCurrent = new Chart(document.getElementById('chart-current'), {
  type: 'line',
  plugins: [progressLinePlugin],
  data: {
    labels: [],
    datasets: [
      {
        label: '实时电流',
        data: [],
        borderColor: '#3fb950', borderWidth: 1.5,
        pointRadius: 0, tension: 0.3,
        fill: true, backgroundColor: 'rgba(121, 192, 255, 0.12)' // 改为淡蓝色填充，与绿色曲线区分
      },
      {
        label: '移动平均',
        data: [],
        borderColor: '#79c0ff', borderWidth: 1.5,
        pointRadius: 0, tension: 0.3,
        borderDash: [6, 4],
        fill: false
      }
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    layout: { padding: { top: 6, right: 8, bottom: 16, left: 8 } },
    plugins: { legend: { display: true }, tooltip: { callbacks: { label: c => `${c.dataset.label}: ${c.parsed.y.toFixed(3)} mA` } } },
    scales: {
      x: { ticks: { color:'#7d8590', font:{size:10}, maxTicksLimit:8, autoSkip:true }, grid:{color:'#21262d'}, border:{color:'#30363d'} },
      y: { ticks: { color:'#7d8590', font:{size:10}, maxTicksLimit:6, stepSize: 10 }, grid:{color:'#21262d'}, border:{color:'#30363d'}, min: 0, max: 100 }
    }
  }
});

function timeLabel(ts) {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

function dateTimeLabel(ts) {
  const d = new Date(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${timeLabel(ts)}`;
}

// ── 更新曲线 ───────────────────────────────────────────────────────────────
function updateChart() {
  const now = Date.now();
  if (now - lastChartUpdate < CHART_UPDATE_INTERVAL) return;
  lastChartUpdate = now;

  if (!data.length) {
    chartCurrent.data.labels = [];
    chartCurrent.data.datasets[0].data = [];
    chartCurrent.data.datasets[1].data = [];
    chartCurrent.update('none');
    return;
  }

  windowCycles = parseInt(document.getElementById('window-cycles').value, 10) || 1;
  const cyclePointCount = latestStats?.cycle_point_count || 1;
  const pointCount = Math.max(1, windowCycles * cyclePointCount);

  // 恢复滑动窗口模式：保持连续绘制，填满后旧数据向左缓慢退出
  const win = data.slice(-pointCount);

  const labels = win.map(d => timeLabel(d.ts));
  const currentData = win.map(d => d.current);
  const avgData = win.map(d => d.stats?.moving_avg_current_ma ?? null);

  // 补齐未满一个显示窗口的数据，使 X 轴（时间跨度）保持固定长度
  if (win.length > 0 && win.length < pointCount) {
    const missing = pointCount - win.length;
    const lastTs = win[win.length - 1].ts;
    for (let i = 1; i <= missing; i++) {
      labels.push(timeLabel(lastTs + i * SAMPLE_INTERVAL_MS));
      currentData.push(null);
      avgData.push(null);
    }
  }

  chartCurrent.data.labels = labels;
  chartCurrent.data.datasets[0].data = currentData;
  chartCurrent.data.datasets[1].data = avgData;

  chartCurrent.options.scales.y.max = latestStats?.chart_y_max || 100;
  chartCurrent.options.scales.y.min = 0;

  chartCurrent.update('none');
}

// ── 渲染后端计算结果 ───────────────────────────────────────────────────────
function setValue(id, value, unit = '', digits = 2) {
  document.getElementById(id).innerHTML = value === null || value === undefined
    ? `--${unit ? `<span>${unit}</span>` : ''}`
    : `${value.toFixed(digits)}${unit ? `<span>${unit}</span>` : ''}`;
}

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function formatEnergy(mwh) {
  if (mwh === null || mwh === undefined) return { value: '--', unit: 'mWh' };
  return mwh >= 1
    ? { value: mwh.toFixed(3), unit: 'mWh' }
    : { value: (mwh * 1000).toFixed(2), unit: 'μWh' };
}

function formatBatteryLife(hours) {
  if (hours === null || hours === undefined) return '--';
  if (hours >= 24 * 30) return `${(hours / 24 / 30).toFixed(1)}<span>月</span>`;
  if (hours >= 24) return `${(hours / 24).toFixed(1)}<span>天</span>`;
  return `${hours.toFixed(1)}<span>h</span>`;
}

function renderMetrics(pt) {
  const stats = pt.stats || {};
  latestStats = stats;

  const el = document.getElementById('val-current');
  el.className = 'metric-value ' + (pt.current >= 0 ? 'green' : 'red');
  el.innerHTML = `${pt.current >= 0 ? '+' : ''}${pt.current.toFixed(2)}<span>mA</span>`;

  document.getElementById('val-power').innerHTML = `${pt.power.toFixed(2)}<span>mW</span>`;
  setValue('val-peak', stats.peak_current_ma, 'mA');
  setValue('val-valley', stats.valley_current_ma, 'mA');
  setValue('r-avg-power', stats.avg_power_mw, 'mW');
  setText('r-avg-display', stats.avg_current_ma === null || stats.avg_current_ma === undefined
    ? '等待数据...'
    : `${stats.avg_current_ma.toFixed(3)} mA`);
  setText('r-sample-count', stats.sample_count || 0);
  setText('r-sample-duration', stats.sample_duration_s || 0);

  const totalEnergy = formatEnergy(stats.total_energy_mwh);
  document.getElementById('val-total-energy').innerHTML = `${totalEnergy.value}<span>${totalEnergy.unit}</span>`;

  if (stats.avg_active_period_s === null || stats.avg_active_period_s === undefined) {
    setText('r-label-sample-time', '平均活跃时长');
    document.getElementById('r-sample-active-time').innerHTML = `--<span>s</span>`;
    setText('r-sample-duration-sub', '等待一次完整活跃周期后更新');
  } else {
    setText('r-label-sample-time', `平均活跃时长(${stats.active_period_count})`);
    document.getElementById('r-sample-active-time').innerHTML = `${stats.avg_active_period_s.toFixed(2)}<span>s</span>`;
    setText('r-sample-duration-sub', `电流>${stats.active_threshold_ma}mA 到 ≤${stats.active_threshold_ma}mA 的平均时长`);
  }

  const cycleCount = stats.completed_cycle_count || 0;
  if (!cycleCount) {
    setText('r-label-cycle-avg', '平均周期电流');
    setText('r-label-sample-power', '平均单次能耗');
    setText('r-label-total-cycles', '平均支持次数');
    setText('r-label-target-life', '平均预估续航');
    document.getElementById('r-cycle-avg-current').innerHTML = `--<span>mA</span>`;
    document.getElementById('r-sample-power').innerHTML = `--<span>mWh</span>`;
    setText('r-sample-power-sub', '等待一个完整采集周期后更新');
    document.getElementById('r-total-cycles').innerHTML = `--<span>次</span>`;
    setText('r-cycles-sub', '等待一个完整采集周期后更新');
    document.getElementById('r-target-life').innerHTML = '--';
    setText('r-target-sub', '等待一个完整采集周期后更新');
    return;
  }

  setText('r-label-cycle-avg', `平均周期电流(${cycleCount})`);
  setText('r-label-sample-power', `平均单次能耗(${cycleCount})`);
  setText('r-label-total-cycles', `平均支持次数(${cycleCount})`);
  setText('r-label-target-life', `平均预估续航(${cycleCount})`);
  setValue('r-cycle-avg-current', stats.avg_cycle_current_ma, 'mA');

  const cycleEnergy = formatEnergy(stats.avg_cycle_energy_mwh);
  document.getElementById('r-sample-power').innerHTML = `${cycleEnergy.value}<span>${cycleEnergy.unit}</span>`;
  setText('r-sample-power-sub', '平均每次采集周期能耗');

  const supportCount = stats.support_count || 0;
  document.getElementById('r-total-cycles').innerHTML = `${supportCount.toLocaleString()}<span>次</span>`;
  setText('r-cycles-sub', `按采集周期平均能耗 · 支持 ${supportCount.toLocaleString()} 次`);
  document.getElementById('r-target-life').innerHTML = formatBatteryLife(stats.estimated_life_hours);
  setText('r-target-sub', stats.estimated_life_hours === null || stats.estimated_life_hours === undefined
    ? '周期能耗为 0，暂无法估算续航'
    : `目标周期 ${stats.target_cycle_seconds}s · 对应 ${stats.estimated_life_hours.toFixed(1)} 小时`);
}

function sendControl(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(payload));
}

function sendSettings() {
  sendControl({
    type: 'settings',
    device_id: document.getElementById('device-id').value.trim(),
    battery_mAh: parseFloat(document.getElementById('battery').value) || 0,
    current_cycle_s: parseFloat(document.getElementById('t-cycle-current').value) || 0,
    target_cycle_s: parseFloat(document.getElementById('t-cycle-target').value) || 0
  });
}

function currentSettingsPayload(type) {
  return {
    type,
    device_id: document.getElementById('device-id').value.trim(),
    battery_mAh: parseFloat(document.getElementById('battery').value) || 0,
    current_cycle_s: parseFloat(document.getElementById('t-cycle-current').value) || 0,
    target_cycle_s: parseFloat(document.getElementById('t-cycle-target').value) || 0
  };
}

function csvValue(value) {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

['device-id', 'battery', 't-cycle-current', 't-cycle-target'].forEach(id => {
  document.getElementById(id).addEventListener('input', () => {
    if (monitoringStarted) sendSettings();
    updateChart();
  });
});

function selectWorkflow(mode) {
  const isMonitor = mode === 'monitor';
  document.getElementById('monitor-workflow').classList.toggle('hidden', !isMonitor);
  document.getElementById('query-workflow').classList.toggle('hidden', isMonitor);
  document.getElementById('mode-monitor').classList.toggle('active', isMonitor);
  document.getElementById('mode-query').classList.toggle('active', !isMonitor);
  document.getElementById('workflow-status').textContent = isMonitor
    ? '监测入口 · 输入设备编号后开始'
    : '查询入口 · 输入设备编号后查询';
}

function startMonitoring() {
  const deviceId = document.getElementById('device-id').value.trim();
  if (!deviceId) {
    document.getElementById('monitor-start-status').textContent = '设备编号不能为空';
    document.getElementById('device-id').focus();
    return;
  }
  clearDisplayData();
  document.getElementById('monitor-start-status').textContent = '正在开始监测...';
  sendControl(currentSettingsPayload('start_monitor'));
}

function stopMonitoring() {
  monitoringStarted = false;
  document.getElementById('monitor-start-status').textContent = '已停止监测';
  sendControl({type: 'stop_monitor'});
}

// ── WebSocket ──────────────────────────────────────────────────────────────
function connect() {
  ws = new WebSocket(WS_URL);
  
  ws.onopen = () => {
    console.log('[WebSocket] ✓ 已连接');
    document.getElementById('status-dot').className = '';
    document.getElementById('status-text').textContent = '已连接 · 请选择流程';
  };
  
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'control_error') {
        document.getElementById('workflow-status').textContent = msg.message || '操作失败';
        document.getElementById('monitor-start-status').textContent = msg.message || '操作失败';
        document.getElementById('life-query-status').textContent = msg.message || '操作失败';
        return;
      }
      if (msg.type === 'monitor_started') {
        monitoringStarted = true;
        document.getElementById('monitor-start-status').textContent = `正在监测设备 ${msg.device_id}`;
        document.getElementById('status-text').textContent = `监测中 · ${msg.device_id}`;
        return;
      }
      if (msg.type === 'monitor_stopped') {
        monitoringStarted = false;
        document.getElementById('status-text').textContent = '已连接 · 监测已停止';
        return;
      }
      if (msg.type === 'life_query_result') {
        renderLifeQueryResults(msg.records || []);
        return;
      }
      const pt = msg;
      data.push(pt);
      if (data.length > MAX_POINTS) data.shift();
      renderMetrics(pt);
      updateChart();
    } catch (err) {
      console.error('[WebSocket] 数据解析错误:', err);
    }
  };
  
  ws.onclose = () => {
    console.warn('[WebSocket] 连接已断开');
    document.getElementById('status-dot').className = 'disconnected';
    document.getElementById('status-text').textContent = '断开 · 5s后重连...';
    setTimeout(connect, 5000);
  };
  
  ws.onerror = (err) => {
    console.error('[WebSocket] 连接错误:', err);
    document.getElementById('status-dot').className = 'disconnected';
    document.getElementById('status-text').textContent = '❌ 连接失败 · 检查服务器';
    // 显示更详细的错误提示
    const hint = document.createElement('div');
    hint.style.cssText = 'position:fixed;top:20px;right:20px;background:#f85149;color:#fff;padding:12px 16px;border-radius:8px;font-size:12px;z-index:9999;max-width:300px;';
    hint.innerHTML = `
      <div style="font-weight:bold;margin-bottom:8px;">⚠️ WebSocket 连接失败</div>
      <div style="margin-bottom:4px;">检查清单:</div>
      <div>1. Python 服务器是否启动?</div>
      <div>2. 运行: <code style="background:#000;padding:2px 4px;border-radius:3px;">python3 backend/server.py</code></div>
      <div style="margin-top:8px;font-size:11px;color:#ddd;">若无硬件，运行: <code style="background:#000;padding:2px 4px;">DEBUG=1 python3 backend/server.py</code></div>
    `;
    document.body.appendChild(hint);
    setTimeout(() => hint.remove(), 8000);
    ws.close();
  };
}

function queryDeviceLife() {
  const deviceId = document.getElementById('query-device-id').value.trim();
  if (!deviceId) {
    document.getElementById('life-query-status').textContent = '查询设备编号不能为空';
    document.getElementById('query-device-id').focus();
    return;
  }
  document.getElementById('life-query-status').textContent = '查询中...';
  sendControl({type: 'query_life', device_id: deviceId});
}

function renderLifeQueryResults(records) {
  const body = document.getElementById('life-query-body');
  if (!records.length) {
    body.innerHTML = '<tr><td colspan="7" class="muted">没有找到匹配的续航记录</td></tr>';
    document.getElementById('life-query-status').textContent = '查询完成 · 0 条';
    return;
  }

  body.innerHTML = records.map(record => {
    const cycleEnergy = formatEnergy(record.avg_cycle_energy_mwh);
    return `
      <tr>
        <td>${escapeHtml(record.device_id || '未设置')}</td>
        <td>${dateTimeLabel(record.ts_ms)}</td>
        <td>${record.sample_duration_s ?? 0}s</td>
        <td>${formatNumber(record.avg_current_ma, 3)} mA</td>
        <td>${cycleEnergy.value} ${cycleEnergy.unit}</td>
        <td>${record.support_count === null || record.support_count === undefined ? '--' : Number(record.support_count).toLocaleString()}</td>
        <td>${formatPlainLife(record.estimated_life_hours)}</td>
      </tr>
    `;
  }).join('');
  document.getElementById('life-query-status').textContent = `查询完成 · ${records.length} 条`;
}

function formatNumber(value, digits = 2) {
  return value === null || value === undefined ? '--' : Number(value).toFixed(digits);
}

function formatPlainLife(hours) {
  if (hours === null || hours === undefined) return '--';
  if (hours >= 24 * 30) return `${(hours / 24 / 30).toFixed(1)} 月`;
  if (hours >= 24) return `${(hours / 24).toFixed(1)} 天`;
  return `${Number(hours).toFixed(1)} h`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[char]));
}

function clearData() {
  clearDisplayData();
  sendControl({type: 'reset_all'});
  if (monitoringStarted) sendSettings();
}

function clearDisplayData() {
  data = [];
  latestStats = null;
  document.getElementById('val-peak').innerHTML = `--<span>mA</span>`;
  document.getElementById('val-valley').innerHTML = `--<span>mA</span>`;
  document.getElementById('val-total-energy').innerHTML = `--<span>mWh</span>`;
  document.getElementById('r-avg-power').innerHTML = `--<span>mW</span>`;
  document.getElementById('r-avg-display').textContent = '等待数据...';
  document.getElementById('r-sample-count').textContent = '0';
  document.getElementById('r-sample-duration').textContent = '0';
  chartCurrent.data.labels = [];
  chartCurrent.data.datasets[0].data = [];
  chartCurrent.data.datasets[1].data = [];
  chartCurrent.update();
}

function resetPeak() {
  document.getElementById('val-peak').innerHTML = `--<span>mA</span>`;
  sendControl({type: 'reset_peak'});
}

function exportCSV() {
  if (!data.length) return;
  const rows = ['device_id,timestamp,current_mA,power_mW,voltage_V'];
  data.forEach(d => rows.push([
    csvValue(d.device_id),
    new Date(d.ts).toISOString(),
    d.current,
    d.power,
    d.voltage
  ].join(',')));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([rows.join('\n')], {type:'text/csv'}));
  a.download = `esp32s3_power_${Date.now()}.csv`;
  a.click();
}

function toggleFullScreen() {
  const fsButton = document.getElementById('fs-btn');
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen()
      .then(() => { fsButton.textContent = '退出全屏'; })
      .catch(err => alert(`进入全屏失败：${err.message}`));
  } else {
    document.exitFullscreen()
      .then(() => { fsButton.textContent = '全屏'; })
      .catch(err => alert(`退出全屏失败：${err.message}`));
  }
}

document.addEventListener('fullscreenchange', () => {
  const fsButton = document.getElementById('fs-btn');
  fsButton.textContent = document.fullscreenElement ? '退出全屏' : '全屏';
});

document.getElementById('window-cycles').addEventListener('input', () => {
  windowCycles = parseInt(document.getElementById('window-cycles').value, 10) || 10;
  updateChart();
});

connect();
