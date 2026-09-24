type AvatarProps = { name: string | null; size?: number };

export function Avatar({ name, size = 32 }: AvatarProps) {
  const initials = (name ?? "?").slice(0, 2).toUpperCase();
  return (
    <span className="avatar" style={{ width: size, height: size }}>
      {initials}
    </span>
  );
}
