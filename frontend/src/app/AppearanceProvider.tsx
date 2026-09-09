import { Alert, App as AntApp, ConfigProvider, Spin } from "antd";
import zhCN from "antd/locale/zh_CN";
import { createContext, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { getSettings, updateAppearanceConfig, type AppearanceMode, type UserSettings } from "../api/settings";
import { applyAppearance, appearanceTheme } from "./theme";

interface AppearanceState {
  mode: AppearanceMode;
  saving: boolean;
  error: string;
  initialSettings: UserSettings | null;
  setMode: (mode: AppearanceMode) => Promise<void>;
}

const AppearanceContext = createContext<AppearanceState | null>(null);

export function useAppearance() {
  const value = useContext(AppearanceContext);
  if (!value) throw new Error("AppearanceProvider is required");
  return value;
}

export function useAppearanceMode() {
  return useContext(AppearanceContext)?.mode ?? "light";
}

export function useInitialSettings() {
  return useContext(AppearanceContext)?.initialSettings ?? null;
}

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [mode, updateMode] = useState<AppearanceMode>("light");
  const [initialSettings, setInitialSettings] = useState<UserSettings | null>(null);
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const savingRef = useRef(false);
  const config = useMemo(() => appearanceTheme(mode), [mode]);

  useEffect(() => {
    let active = true;
    void getSettings().then((settings) => {
      if (!active) return;
      setInitialSettings(settings);
      updateMode(settings.appearance_config.mode);
    }).catch(() => {
      if (active) setLoadError(true);
    }).finally(() => {
      if (active) setReady(true);
    });
    return () => { active = false; };
  }, []);

  useLayoutEffect(() => { if (ready) applyAppearance(mode); }, [mode, ready]);

  async function setMode(next: AppearanceMode) {
    if (savingRef.current || next === mode) return;
    const previous = mode;
    savingRef.current = true;
    setSaving(true);
    setError("");
    updateMode(next);
    try {
      const saved = await updateAppearanceConfig(next);
      updateMode(saved.mode);
      setLoadError(false);
    } catch {
      updateMode(previous);
      setError("外观保存失败，已恢复之前的模式，请重试。");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  return (
    <AppearanceContext.Provider value={{ mode, saving, error, initialSettings, setMode }}>
      <ConfigProvider locale={zhCN} theme={config}>
        <AntApp>
          {!ready ? <div className="appearance-loading"><Spin aria-label="加载外观" /></div> : <>
            {loadError ? <Alert className="appearance-load-error" type="warning" showIcon
              title="设置读取失败，暂时使用浅色模式。请刷新重试。" closable /> : null}
            {children}
          </>}
        </AntApp>
      </ConfigProvider>
    </AppearanceContext.Provider>
  );
}
