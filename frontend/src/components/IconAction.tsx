import { Button, Tooltip, type ButtonProps } from "antd";
import type { ReactNode } from "react";
import { useButtonTooltips } from "./ButtonTooltipContext";

type IconActionProps = Omit<ButtonProps, "aria-label" | "children" | "icon"> & {
  label: string;
  icon: ReactNode;
};

export default function IconAction({ label, icon, size = "small", ...props }: IconActionProps) {
  const showTooltips = useButtonTooltips();
  const button = (
      <Button
        {...props}
        className={["icon-action", props.className].filter(Boolean).join(" ")}
        type={props.type ?? "text"}
        size={size}
        icon={icon}
        aria-label={label}
      />
  );
  return showTooltips ? <Tooltip title={label} placement="top">{button}</Tooltip> : button;
}

export type { IconActionProps };
