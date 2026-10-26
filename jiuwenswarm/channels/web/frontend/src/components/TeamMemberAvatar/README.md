# TeamMemberAvatar 使用说明

`TeamMemberAvatar` 是团队成员头像的统一渲染组件。需要展示成员头像时，优先使用这个组件，不要在业务组件里重新写头像哈希或导入 `Team-*.svg`。

## 基本用法

```tsx
import { TeamMemberAvatar } from '../TeamMemberAvatar';

<TeamMemberAvatar member={member.member_id} />
```

从 `components/ChatPanel` 这类子目录引用时：

```tsx
import { TeamMemberAvatar } from '../TeamMemberAvatar';
```

从 `components/MemberTaskDrawer` 这类子目录引用时：

```tsx
import { TeamMemberAvatar } from '../TeamMemberAvatar';
```

## member 传什么

`member` 传成员身份标识，优先使用后端事件或 store 中已有的成员 ID：

```tsx
<TeamMemberAvatar member={member.member_id} />
<TeamMemberAvatar member={event.fromMember} />
<TeamMemberAvatar member="team_leader" />
<TeamMemberAvatar member="user" />
<TeamMemberAvatar member="ethan" />
```

对应后端字段通常是：

- `member_id`
- `from_member`
- 前端解析后的 `fromMember`

不要传展示名、角色名或任务名。

## 当前头像规则

头像解析逻辑在 `src/utils/teamMemberAvatar.ts`：

- 先对传入值做规范化：去掉首尾空格、转小写、把空格和 `-` 转成 `_`。
- `team_leader` 和 `teamleader` 使用 `teamleader.svg`。
- `user` 使用 `user-in-team.svg`。
- 其他普通成员使用规范化后的 ID 做稳定哈希，从 `Team-2.svg` 到 `Team-6.svg` 中选择一张。

因此同一个成员 ID 在消息区、团队区、任务抽屉里会得到同一个头像。

## 自定义尺寸和形状

组件默认是 `h-9 w-9`，消息区可以直接使用：

```tsx
<TeamMemberAvatar member={event.fromMember} />
```

如果业务位置需要圆形头像或不同尺寸，传 `className` 和 `imageClassName`：

```tsx
<TeamMemberAvatar
  member={member.member_id}
  alt={displayName}
  className="h-10 w-10 rounded-full"
  imageClassName="rounded-full"
/>
```

`className` 作用在外层容器上，`imageClassName` 作用在内部 `<img>` 上。

## 只获取头像地址

如果不需要渲染组件，只需要图片地址：

```ts
import { getTeamMemberAvatarSrc } from '../../utils/teamMemberAvatar';

const avatarSrc = getTeamMemberAvatarSrc(memberId);
```

如果需要知道头像类型和规范化后的 ID：

```ts
import { resolveTeamMemberAvatar } from '../../utils/teamMemberAvatar';

const avatar = resolveTeamMemberAvatar(memberId);

avatar.src;
avatar.kind; // 'leader' | 'user' | 'member'
avatar.normalizedId;
```

## 约定

- 头像不写入 WebSocket payload、store 或 session 数据。
- 头像是由成员 ID 在渲染时确定性派生出来的。
- 新增头像使用场景时，应复用 `TeamMemberAvatar` 或 `teamMemberAvatar.ts` 中的工具函数。
