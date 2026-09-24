import { Avatar } from "@/components/Avatar";
import { getUser } from "@/server/users";
import { useState } from "react";

export const useProfileTab = (initial: string) => {
  const [tab, setTab] = useState(initial);
  return { tab, setTab };
};

/** Server component: renders the profile of the current user. */
export default async function ProfilePage({ params }: { params: { id: string } }) {
  const user = await getUser(params.id);
  if (!user) return <p>Not found</p>;
  return (
    <main>
      <Avatar name={user.name} />
      <h1>{user.email}</h1>
    </main>
  );
}
