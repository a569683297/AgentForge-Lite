import { beforeEach, describe, expect, it } from 'vitest';
import { rememberModel, resolveModelForSession } from './modelMemory';

/**
 * 会话级模型记忆的单测。
 *
 * 为什么值得测：这套语义（各记各的 / 继承最近 / null 是合法状态）
 * 全在 localStorage 的两个键里进出，没有 UI 兜底 ——
 * 记忆串会话、继承断裂这类 bug 在界面上表现为"下拉框莫名其妙变了"，
 * 是最难从表象反推的一类问题。
 */
describe('modelMemory（会话级模型记忆）', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('空存储：任何会话都解析为 null（默认通道）', () => {
    expect(resolveModelForSession(undefined)).toBeNull();
    expect(resolveModelForSession('session-a')).toBeNull();
  });

  it('新会话页选择 → 更新最近选择，已有/未来会话都继承它', () => {
    rememberModel(undefined, 'deepseek');
    expect(resolveModelForSession(undefined)).toBe('deepseek');
    // "新会话继承最近选择"：还没有自己记录的会话，解析结果 = last
    expect(resolveModelForSession('session-a')).toBe('deepseek');
  });

  it('会话内选择 → 该会话记住；其它会话继承最近选择', () => {
    rememberModel('session-a', 'openai');
    expect(resolveModelForSession('session-a')).toBe('openai');
    expect(resolveModelForSession('session-b')).toBe('openai'); // 继承 last
  });

  it('各会话各记各的：A 选 a、B 选 b 后，A 仍是 a（不被 B 的 last 覆盖）', () => {
    rememberModel('session-a', 'model-a');
    rememberModel('session-b', 'model-b');
    expect(resolveModelForSession('session-a')).toBe('model-a');
    expect(resolveModelForSession('session-b')).toBe('model-b');
  });

  it('显式切回"默认"（null）：会话记录被删除，解析回落到最近选择', () => {
    rememberModel('session-a', 'openai');
    rememberModel('session-a', null);

    // 此时 last 也被置空（"最近选择"就是默认）→ A 解析为 null
    expect(resolveModelForSession('session-a')).toBeNull();
    expect(resolveModelForSession('session-b')).toBeNull();
  });

  it('切回默认只影响该会话的记录：last 置空后，其它会话的显式记录不受影响', () => {
    rememberModel('session-a', 'model-a');
    rememberModel('session-b', 'model-b');
    rememberModel('session-b', null); // B 切回默认

    expect(resolveModelForSession('session-a')).toBe('model-a'); // A 自己的记录还在
    expect(resolveModelForSession('session-b')).toBeNull();
  });

  it('存储损坏（非法 JSON）时按空记忆处理，不抛异常', () => {
    window.localStorage.setItem('af:model:last', '{broken');
    window.localStorage.setItem('af:model:by-session', 'not-json');

    expect(resolveModelForSession('session-a')).toBeNull();
    // 损坏后写入应能自愈：覆盖为合法内容
    rememberModel('session-a', 'deepseek');
    expect(resolveModelForSession('session-a')).toBe('deepseek');
  });

  it('by-session 里是数组/字符串等非对象值时同样按空表处理', () => {
    window.localStorage.setItem('af:model:by-session', '[1,2,3]');
    expect(resolveModelForSession('session-a')).toBeNull();
  });
});
