import React from "react";

interface BullIconProps {
  size: number;
  color?: string;
  style?: React.CSSProperties;
}

// Paths inlined from WeightApp/.../LiftTheBullIcon.imageset/lift_the_bull_icon.svg.
// Same source used in the Remotion visualizations — kept identical so the website
// brand mark renders pixel-for-pixel like the marketing videos.
export const BullIcon: React.FC<BullIconProps> = ({
  size,
  color = "#FFC850",
  style,
}) => {
  const aspect = 14.09 / 10.09;
  const height = size / aspect;
  return (
    <svg
      width={size}
      height={height}
      viewBox="0 0 14.09 10.09"
      xmlns="http://www.w3.org/2000/svg"
      style={style}
      aria-hidden
    >
      <g fill={color}>
        <path d="M8.58,5.71s-.06,0-.09,0l-1.26-2.21.88-1.52,1.91,1.87-1.44,1.85Z" />
        <polygon points="5.93 1.98 6.83 3.5 5.52 5.71 4.07 3.85 5.93 1.98" />
        <polygon points="8.35 6.12 7.95 7.97 6.12 7.95 5.75 6.12 8.35 6.12" />
        <path d="M4,4.34c.5.52.91,1.13,1.38,1.69.11.69.39,1.48.44,2.16.03.43-.03.89,0,1.32l-1.81-3.54v-1.62Z" />
        <path d="M10.06,6l-1.74,3.51c-.06,0-.03-.08-.03-.12-.01-.4-.03-.91,0-1.3.05-.65.33-1.37.4-2.03l1.38-1.72v1.66Z" />
        <polygon points="7.94 8.28 7.94 10.09 6.17 10.09 6.12 10.04 6.12 8.28 7.94 8.28" />
        <polygon points="11.97 1.88 10.24 3.62 8.48 1.88 11.97 1.88" />
        <polygon points="14.09 1.88 12.3 3.69 10.64 3.69 10.69 3.59 12.4 1.88 14.09 1.88" />
        <polygon points="5.58 1.88 3.85 3.63 2.12 1.88 5.58 1.88" />
        <polygon points="3.45 3.69 1.8 3.69 0 1.88 1.66 1.88 3.45 3.69" />
        <polygon points="5.88 5.81 7.06 3.83 8.21 5.81 5.88 5.81" />
        <polygon points="1.57 1.57 0 1.57 1.57 0 1.57 1.57" />
        <polygon points="14.09 1.57 12.52 1.57 12.52 0 14.09 1.57" />
      </g>
    </svg>
  );
};

export default BullIcon;
