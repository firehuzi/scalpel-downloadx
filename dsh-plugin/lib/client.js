// dsh-downloadx — browser half（手写 bundle，零构建依赖）。
//
// DSH 0.2.x 客户端模块系统契约：
//   * 顶层调用 window.__ModuleLoader__.load({ id, factory })；
//   * factory(require) 返回模块导出（Cordis 客户端插件面）；
//   * require() 解析 shell 冻结的平台表（react / slots / ...）。
//
// 本文件注册两个槽位：
//   * 'sidebar.panellist' 列表 → 侧边栏「下载器」入口（id = 面板 id）；
//   * 'main' 键控槽位         → 该 id 对应的面板主体。
// 面板主体优先通过宿主路由 /api/downloadx/info 查询引擎状态：
//   - 引擎在运行 → iframe 嵌入 DownloadX 自带 Web UI；
//   - 引擎未运行 → 显示「启动 DownloadX 引擎」按钮（POST /api/downloadx/open）；
//   - 取不到宿主路由（如 file:// 环境的桌面壳）→ 退化为直接 iframe 默认地址。
window.__ModuleLoader__.load({
  id: 'dsh-downloadx',
  factory: (require) => {
    const module = { exports: {} };
    const exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' });

    const React = require('react');
    const h = React.createElement;

    /** 面板 id：侧边栏列表 id 与 main 键控槽位 key 必须一致。 */
    const PANEL_ID = 'downloadx';
    /** DownloadX 默认 Web UI（与宿主插件默认 baseUrl 一致）。 */
    const DEFAULT_UI_URL = 'http://127.0.0.1:8720/';
    /** 用户可覆盖 iframe 地址（非 8720 端口时）：localStorage['dsh-downloadx.ui-url']。 */
    const URL_OVERRIDE_KEY = 'dsh-downloadx.ui-url';
    const FONT = '12px';

    function readUrlOverride() {
      try {
        const value = window.localStorage.getItem(URL_OVERRIDE_KEY);
        return value && value.trim() ? value.trim() : '';
      } catch {
        return '';
      }
    }

    function normalizeUiUrl(url) {
      const text = typeof url === 'string' && url.trim() ? url.trim() : DEFAULT_UI_URL;
      return text.endsWith('/') ? text : `${text}/`;
    }

    function writeUrlOverride(url) {
      try {
        window.localStorage.setItem(URL_OVERRIDE_KEY, normalizeUiUrl(url));
      } catch {
        /* 忽略 */
      }
    }

    function prettyError(error) {
      if (!error) return '未知错误';
      if (typeof error === 'string') return error;
      return error.message || String(error);
    }

    /** 带超时的同源 JSON 请求；file:// 环境下会直接失败，由调用方退化处理。 */
    async function fetchJson(path, init = {}, timeoutMs = 4000) {
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(path, {
          ...init,
          signal: controller.signal,
          credentials: 'same-origin',
          headers: { ...(init.headers || {}) },
        });
        const text = await response.text();
        let data = null;
        if (text) {
          try {
            data = JSON.parse(text);
          } catch {
            data = null;
          }
        }
        if (!response.ok) {
          throw new Error((data && (data.error || data.detail)) || `HTTP ${response.status}`);
        }
        return data;
      } finally {
        window.clearTimeout(timer);
      }
    }

    /** 按钮样式（跟随 DSH 主题变量）。 */
    function buttonStyle(primary) {
      return {
        padding: '4px 12px',
        borderRadius: 6,
        border: `1px solid var(--dsw-alias-border-l1, #2a2f3a)`,
        background: primary ? 'var(--dsw-alias-state-accent-primary, #4f8ef7)' : 'transparent',
        color: primary ? '#fff' : 'inherit',
        fontSize: FONT,
        cursor: 'pointer',
        whiteSpace: 'nowrap',
      };
    }

    /** 侧边栏图标：下载箭头 + 托盘，单色（currentColor）。 */
    function DownloadxIcon(props) {
      const size = (props && props.size) || 16;
      const stroke = props && props.active ? 'var(--dsw-alias-brand-primary, currentColor)' : 'currentColor';
      return h(
        'svg',
        {
          width: size,
          height: size,
          viewBox: '0 0 24 24',
          fill: 'none',
          stroke,
          strokeWidth: 1.8,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': 'true',
        },
        h('path', { d: 'M12 4v10' }),
        h('path', { d: 'm7.5 10.5 4.5 4.5 4.5-4.5' }),
        h('path', { d: 'M5 19h14' }),
      );
    }

    /** 引擎未运行时的引导卡片。 */
    function EngineDownCard(props) {
      const box = {
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 10,
        height: '100%',
        padding: 24,
        textAlign: 'center',
      };
      const muted = { fontSize: FONT, opacity: 0.6, maxWidth: 420, lineHeight: 1.7 };
      return h(
        'div',
        { style: box },
        h('div', { style: { fontSize: 15, fontWeight: 600 } }, 'DownloadX 引擎未运行'),
        h(
          'div',
          { style: muted },
          '下载引擎是一个独立的本地服务；启动后这里会直接嵌入它的 Web UI。',
          h('br'),
          '也可以点「在浏览器打开」，或先在终端运行：python -m downloadx serve',
        ),
        props.error ? h('div', { style: { ...muted, color: 'var(--dsw-alias-state-danger, #f85149)' } }, props.error) : null,
        h(
          'div',
          { style: { display: 'flex', gap: 8 } },
          h(
            'button',
            { type: 'button', style: buttonStyle(true), onClick: props.onStart, disabled: props.starting },
            props.starting ? '正在启动…' : '启动 DownloadX 引擎',
          ),
          h('button', { type: 'button', style: buttonStyle(false), onClick: props.onOpenBrowser }, '在浏览器打开'),
        ),
        h('div', { style: muted }, `地址：${props.uiUrl}`),
      );
    }

    /**
     * 面板主体：工具条 + DownloadX Web UI。
     * 生命周期：mount 时询问宿主 → 决定 iframe / 引导卡片；按钮可刷新与启动引擎。
     */
    function DownloadxPage() {
      const override = readUrlOverride();
      const [phase, setPhase] = React.useState('checking'); // checking | running | down | direct
      const [engineUrl, setEngineUrl] = React.useState(normalizeUiUrl(override || DEFAULT_UI_URL));
      const [nonce, setNonce] = React.useState(0);
      const [starting, setStarting] = React.useState(false);
      const [error, setError] = React.useState('');
      const [status, setStatus] = React.useState('正在检查引擎…');

      const probeHost = React.useCallback(async () => {
        setStatus('正在检查引擎…');
        setError('');
        try {
          const info = await fetchJson('/api/downloadx/info');
          const url = normalizeUiUrl(info && (info.uiUrl || (info.engine && info.engine.baseUrl)));
          setEngineUrl(url);
          if (info && info.engine && info.engine.running) {
            setPhase('running');
            setStatus('');
            setNonce((value) => value + 1);
          } else {
            setPhase('down');
            setStatus('');
          }
        } catch {
          // 无宿主路由（例如 Electron file:// 壳）：不阻塞，直接 iframe 默认地址。
          setPhase('direct');
          setStatus('');
        }
      }, []);

      React.useEffect(() => {
        let cancelled = false;
        probeHost().finally(() => {
          if (cancelled) return;
        });
        return () => {
          cancelled = true;
        };
      }, [probeHost]);

      const startEngine = async () => {
        setStarting(true);
        setError('');
        try {
          const data = await fetchJson('/api/downloadx/open', { method: 'POST' }, 60000);
          const url = normalizeUiUrl(data && data.uiUrl);
          setEngineUrl(url);
          setPhase('running');
          setNonce((value) => value + 1);
        } catch (err) {
          setError(`启动失败：${prettyError(err)}`);
        } finally {
          setStarting(false);
        }
      };

      const openBrowser = () => {
        try {
          window.open(engineUrl, '_blank', 'noopener,noreferrer');
        } catch {
          /* 忽略 */
        }
      };

      const useCustomUrl = () => {
        const input = window.prompt('DownloadX Web UI 地址（写进 localStorage，可留空恢复默认）', engineUrl);
        if (input === null) return;
        const next = input.trim();
        if (next) {
          writeUrlOverride(next);
          setEngineUrl(normalizeUiUrl(next));
          setPhase('running');
        } else {
          try {
            window.localStorage.removeItem(URL_OVERRIDE_KEY);
          } catch {
            /* 忽略 */
          }
          setEngineUrl(normalizeUiUrl(DEFAULT_UI_URL));
          setPhase('direct');
        }
        setNonce((value) => value + 1);
      };

      const toolbarStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 10px',
        borderBottom: '1px solid var(--dsw-alias-border-l1, #2a2f3a)',
        fontSize: FONT,
        flex: 'none',
      };
      const labelStyle = {
        flex: '1 1 auto',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        opacity: 0.62,
      };
      const body = () => {
        if (phase === 'checking') {
          return h('div', { style: { padding: 20, fontSize: FONT, opacity: 0.6 } }, status || '正在检查引擎…');
        }
        if (phase === 'down') {
          return h(EngineDownCard, { uiUrl: engineUrl, starting, error, onStart: startEngine, onOpenBrowser: openBrowser });
        }
        return h('iframe', {
          key: `${engineUrl}#${nonce}`,
          src: engineUrl,
          title: 'DownloadX',
          allow: 'clipboard-read; clipboard-write',
          style: {
            position: 'absolute',
            inset: 0,
            width: '100%',
            height: '100%',
            border: '0',
            background: '#0b0e14',
          },
        });
      };

      return h(
        'div',
        { style: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 } },
        h(
          'div',
          { style: toolbarStyle },
          h('strong', { style: { fontWeight: 600 } }, 'DownloadX'),
          h('span', { style: labelStyle }, phase === 'running' || phase === 'direct' ? engineUrl : '下载器'),
          h('span', { style: { ...labelStyle, flex: '0 1 auto', opacity: 0.55 } }, status),
          h(
            'button',
            {
              type: 'button',
              style: buttonStyle(false),
              onClick: () => {
                setNonce((value) => value + 1);
                probeHost();
              },
              title: '重新检查引擎并刷新面板',
            },
            '刷新',
          ),
          h('button', { type: 'button', style: buttonStyle(false), onClick: useCustomUrl, title: '设置 DownloadX UI 地址' }, '地址'),
          h('button', { type: 'button', style: buttonStyle(false), onClick: openBrowser }, '在浏览器打开'),
        ),
        h('div', { style: { position: 'relative', flex: '1', minHeight: 0 } }, body()),
      );
    }

    /** 客户端服务依赖（Cordis 服务名）。 */
    const inject = ['slots'];

    /**
     * 注册侧边栏行与面板主体。
     * `slots.inject` 是声明式写法：等槽位宿主（ui-sidebar / ui-layout）就绪后再注册。
     */
    function apply(ctx) {
      ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: PANEL_ID }, DownloadxPage));
      ctx.slots.inject('sidebar.panellist', () =>
        ctx.slots.register({ name: 'sidebar.panellist', id: PANEL_ID, order: 100, label: '下载器' }, DownloadxIcon),
      );
    }

    exports.name = 'dsh-downloadx';
    exports.inject = inject;
    exports.apply = apply;
    return module.exports;
  },
});
