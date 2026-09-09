import { MoonOutlined, SunOutlined } from "@ant-design/icons";
import { Alert, Form, Segmented, Typography } from "antd";
import type { AppearanceMode } from "../../api/settings";
import { useAppearance } from "../../app/AppearanceProvider";

export function AppearanceSettingsSection() {
  const { mode, saving, error, setMode } = useAppearance();
  return (
    <Form layout="vertical">
      <Typography.Title level={4}>外观</Typography.Title>
      <Form.Item label="配色模式">
        <Segmented<AppearanceMode>
          aria-label="配色模式"
          name="appearance"
          value={mode}
          disabled={saving}
          options={[
            { value: "light", label: "浅色", icon: <SunOutlined /> },
            { value: "dark", label: "深色", icon: <MoonOutlined /> },
          ]}
          onChange={(next) => void setMode(next)}
        />
      </Form.Item>
      {error ? <Alert type="error" showIcon title={error} /> : null}
    </Form>
  );
}
