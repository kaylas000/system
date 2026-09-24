import { NextResponse } from "next/server";
import { getUser } from "@/server/users";

export async function GET(request: Request) {
  const id = new URL(request.url).searchParams.get("id") ?? "";
  const user = await getUser(id);
  if (!user) return NextResponse.json({ error: "not found" }, { status: 404 });
  return NextResponse.json(user);
}
