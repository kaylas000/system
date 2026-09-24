import { db } from "./db";

export interface UserDTO {
  id: string;
  email: string;
  name: string | null;
}

/**
 * Loads a single user by id from the database.
 * Returns null when the user does not exist.
 */
export async function getUser(id: string): Promise<UserDTO | null> {
  const user = await db.user.findUnique({ where: { id } });
  return user ? { id: user.id, email: user.email, name: user.name } : null;
}

export async function updateUserName(id: string, name: string): Promise<UserDTO> {
  const existing = await getUser(id);
  if (!existing) throw new Error("User not found");
  await db.user.update({ where: { id }, data: { name } });
  return { ...existing, name };
}
