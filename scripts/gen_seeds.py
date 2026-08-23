#!/usr/bin/env python3
"""生成种子题 JSON：期望输出由 Python 参考解实际运行生成（保证自洽），
随后用 C++ 参考解交叉验证（必须与 Python 输出完全一致才算通过）。

用法：.venv/bin/python scripts/gen_seeds.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from prepdojo.judge import judge_submission  # noqa: E402

P = []  # (id, title, difficulty, tags, statement, inputs, ref_py, ref_cpp)

# 仅标记算法训练上的对应关系；题面和用例仍是 PrepDojo 自写变式。
# 优先级：1=必刷，2=高频，3=补充。
INTERVIEW_META = {
    "cp-001": (167, 1),
    "cp-002": (3, 1),
    "cp-003": (53, 2),
    "cp-004": (25, 2),
    "cp-005": (704, 1),
    "cp-006": (20, 1),
    "cp-007": (239, 2),
    "cp-008": (102, 1),
    "cp-009": (200, 2),
    "cp-010": (56, 1),
    "cp-011": (146, 2),
    "cp-012": (912, 2),
    "cp-013": (70, 2),
    "cp-014": (322, 2),
    "cp-015": (104, 2),
    "cp-016": (283, 2),
    "cp-017": (210, 2),
    "cp-018": (347, 2),
    "cp-019": (5, 3),
    "cp-020": (1926, 2),
}

P.append(dict(
    id="cp-001", title="有序数组两数之和", difficulty="easy", tags=["数组", "双指针", "哈希表"],
    statement="""给定一个 **升序** 整数数组和一个目标值 target，找出两个数使其和恰好等于 target。
保证恰好存在一组解。

**输入格式**：第一行两个整数 n 和 target（2 ≤ n ≤ 10^5）；
第二行 n 个升序整数（绝对值 ≤ 10^9）。

**输出格式**：一行两个整数，为这两个数的下标（从 1 开始，小的在前）。""",
    inputs=["4 9\n2 7 11 15\n", "3 6\n1 2 3\n", "2 0\n-1 1\n", "5 100\n1 20 40 60 80\n", "6 -8\n-10 -6 -3 -1 2 4\n", "2 2000000000\n1000000000 1000000000\n"],
    ref_py="""n,t=map(int,input().split());a=list(map(int,input().split()))
i,j=0,n-1
while i<j:
    s=a[i]+a[j]
    if s==t: print(i+1,j+1); break
    if s<t: i+=1
    else: j-=1
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n,t;std::cin>>n>>t;std::vector<long long>a(n);
for(auto&x:a)std::cin>>x;int i=0,j=n-1;
while(i<j){long long s=a[i]+a[j];if(s==t){std::cout<<i+1<<" "<<j+1<<std::endl;return 0;}
if(s<t)i++;else j--;} }
""",
))

P.append(dict(
    id="cp-002", title="最长无重复字符子串", difficulty="medium", tags=["字符串", "滑动窗口", "哈希表"],
    statement="""给定一个仅含小写英文字母的字符串，求其中不含重复字符的最长连续子串的长度。

**输入格式**：一行字符串 s（1 ≤ |s| ≤ 10^6）。

**输出格式**：一个整数，最长无重复字符子串的长度。""",
    inputs=["abcabcbb\n", "bbbbb\n", "pwwkew\n", "abcdefg\n", "abba\n", "a\n", "tmmzuxt\n", "dvdf\n"],
    ref_py="""s=input().strip()
last={};ans=0;left=0
for i,c in enumerate(s):
    if c in last and last[c]>=left: left=last[c]+1
    last[c]=i
    ans=max(ans,i-left+1)
print(ans)
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){std::string s;std::cin>>s;std::vector<int> last(26,-1);int ans=0,left=0;
for(int i=0;i<(int)s.size();++i){int c=s[i]-'a';
if(last[c]>=left)left=last[c]+1;last[c]=i;ans=std::max(ans,i-left+1);}
std::cout<<ans<<std::endl;}
""",
))

P.append(dict(dict(
    id="cp-003", title="最大子数组和", difficulty="medium", tags=["数组", "动态规划"],
    statement="""给定一个整数数组（可能含负数），求连续子数组的最大和。子数组至少包含一个元素。

**输入格式**：第一行整数 n（1 ≤ n ≤ 10^5）；第二行 n 个整数（绝对值 ≤ 10^4）。

**输出格式**：一个整数，最大子数组和。""",
    inputs=["9\n-2 1 -3 4 -1 2 1 -5 4\n", "1\n1\n", "5\n5 4 -1 7 8\n", "4\n-3 -1 -2 -5\n", "6\n-1 0 -2 0 -3 0\n", "3\n1000000 1000000 1000000\n"],
    ref_py="""n=int(input());a=list(map(int,input().split()))
cur=ans=a[0]
for x in a[1:]:
    cur=max(x,cur+x);ans=max(ans,cur)
print(ans)
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){int n;std::cin>>n;std::vector<long long>a(n);
for(auto&x:a)std::cin>>x;long long cur=a[0],ans=a[0];
for(int i=1;i<n;++i){cur=std::max(a[i],cur+a[i]);ans=std::max(ans,cur);}
std::cout<<ans<<std::endl;}
""",
)))

P.append(dict(
    id="cp-004", title="K 个一组翻转序列", difficulty="medium", tags=["链表", "模拟"],
    statement="""把一个长度为 n 的序列按每 k 个一组分组，组内翻转，不足 k 个的末尾组保持原序。
（本题用序列模拟链表操作，直接用切片会被面试官追问，建议模拟节点指针操作。）

**输入格式**：第一行两个整数 n 和 k（1 ≤ n ≤ 10^5, 1 ≤ k ≤ n）；
第二行 n 个整数。

**输出格式**：一行，处理后的序列，空格分隔。""",
    inputs=["5 2\n1 2 3 4 5\n", "7 3\n1 2 3 4 5 6 7\n", "4 1\n1 2 3 4\n", "6 6\n10 20 30 40 50 60\n", "8 3\n1 2 3 4 5 6 7 8\n", "1 1\n42\n"],
    ref_py="""n,k=map(int,input().split());a=input().split()
out=[]
for i in range(0,n,k):
    g=a[i:i+k]
    out+= g[::-1] if len(g)==k else g
print(" ".join(out))
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n,k;std::cin>>n>>k;std::vector<std::string>a(n);
for(auto&x:a)std::cin>>x;std::vector<std::string>out;
for(long long i=0;i<n;i+=k){long long j=std::min(i+k,n);
if(j-i==k)for(long long t=j-1;t>=i;--t)out.push_back(a[t]);
else for(long long t=i;t<j;++t)out.push_back(a[t]);}
for(size_t i=0;i<out.size();++i)std::cout<<out[i]<<" \\n"[i+1==out.size()];}
""",
))

P.append(dict(
    id="cp-005", title="升序数组二分查找", difficulty="easy", tags=["二分查找"],
    statement="""在一个 **升序** 整数数组上进行 q 次查询，每次给出目标值 x，
若 x 在数组中出现则输出其第一次出现的下标（从 1 开始），否则输出 -1。
要求每次查询时间复杂度 O(log n)。

**输入格式**：第一行两个整数 n 和 q（1 ≤ n, q ≤ 10^5）；
第二行 n 个升序整数；第三行 q 个待查询整数。

**输出格式**：一行 q 个整数，依次对应每个查询的结果，空格分隔。""",
    inputs=["5 3\n1 3 3 5 7\n3 6 1\n", "1 2\n10\n10 10\n", "6 4\n-5 -3 0 2 8 8\n8 -5 9 0\n", "3 1\n1 2 3\n2\n", "4 4\n1 1 1 1\n1 2 0 1\n"],
    ref_py="""import bisect
n,q=map(int,input().split());a=list(map(int,input().split()));qs=list(map(int,input().split()))
res=[]
for x in qs:
    i=bisect.bisect_left(a,x)
    res.append(str(i+1) if i<n and a[i]==x else "-1")
print(" ".join(res))
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n,q;std::cin>>n>>q;std::vector<long long>a(n);
for(auto&x:a)std::cin>>x;
while(q--){long long x;std::cin>>x;
long long lo=0,hi=n;while(lo<hi){long long mid=(lo+hi)/2;if(a[mid]<x)lo=mid+1;else hi=mid;}
if(lo<n&&a[lo]==x)std::cout<<lo+1;else std::cout<<-1;
std::cout<<(q?" ":"\\n");}}
""",
))

P.append(dict(
    id="cp-006", title="括号序列有效性", difficulty="easy", tags=["栈"],
    statement="""判断一个只包含 `(`、`)`、`[`、`]`、`{`、`}` 的字符串是否合法：
左括号必须以正确顺序用相同类型的右括号闭合。

**输入格式**：一行字符串（长度 ≤ 10^5，可能为空行）。

**输出格式**：`valid` 或 `invalid`。""",
    inputs=["()[]{}\n", "([)]\n", "{[]}\n", "(\n", "\n", "((((()))))\n", "(){[]()}\n", "]\n"],
    ref_py="""import sys
s=sys.stdin.readline().rstrip("\\n")
st=[];pair={')':'(',']':'[','}':'{'}
ok=True
for c in s:
    if c in '([{': st.append(c)
    else:
        if not st or st[-1]!=pair.get(c): ok=False;break
        st.pop()
if st: ok=False
print("valid" if ok else "invalid")
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){std::string s;std::getline(std::cin,s);std::stack<char>st;
std::map<char,char>pair{{')','('},{']','['},{'}','{'}};
bool ok=true;
for(char c:s){if(c=='('||c=='['||c=='{')st.push(c);
else{if(st.empty()||st.top()!=pair[c]){ok=false;break;}st.pop();}}
if(!st.empty())ok=false;
std::cout<<(ok?"valid":"invalid")<<std::endl;}
""",
))

P.append(dict(
    id="cp-007", title="滑动窗口最大值", difficulty="hard", tags=["队列", "单调队列", "滑动窗口"],
    statement="""给定长度为 n 的整数数组和窗口大小 k，窗口从最左滑到最右，
输出每个窗口位置的最大值。要求 O(n) 单调队列解法。

**输入格式**：第一行两个整数 n 和 k（1 ≤ k ≤ n ≤ 10^5）；
第二行 n 个整数。

**输出格式**：一行 n-k+1 个整数，空格分隔。""",
    inputs=["8 3\n1 3 -1 -3 5 3 6 7\n", "1 1\n5\n", "5 2\n9 8 7 6 5\n", "6 6\n4 4 4 1 1 1\n", "7 4\n2 -1 3 -5 0 9 -2\n", "3 2\n-100000 -5 -100000\n"],
    ref_py="""from collections import deque
n,k=map(int,input().split());a=list(map(int,input().split()))
dq=deque();out=[]
for i,x in enumerate(a):
    while dq and a[dq[-1]]<=x: dq.pop()
    dq.append(i)
    if dq[0]<=i-k: dq.popleft()
    if i>=k-1: out.append(str(a[dq[0]]))
print(" ".join(out))
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n,k;std::cin>>n>>k;std::vector<long long>a(n);
for(auto&x:a)std::cin>>x;std::deque<long long>dq;
for(long long i=0;i<n;++i){
while(!dq.empty()&&a[dq.back()]<=a[i])dq.pop_back();
dq.push_back(i);
if(dq.front()<=i-k)dq.pop_front();
if(i>=k-1)std::cout<<a[dq.front()]<<(i+1<n?" ":"\\n");}}
""",
))

P.append(dict(
    id="cp-008", title="二叉树每层节点和", difficulty="medium", tags=["二叉树", "广度优先搜索"],
    statement="""一棵二叉树以层序遍历序列给出（缺失子节点用 `null` 占位，见样例），
求每一层的节点值之和。

**输入格式**：一行，层序序列，节点值或 `null`，空格分隔。根为第一个元素；
第 i 个非空节点的左右子节点依次占据序列中的对应空位。
（1 ≤ 节点数 ≤ 10^5，|节点值| ≤ 10^4）

**输出格式**：一行，每层的和，空格分隔。

**样例解释**：输入 `3 9 20 null null 15 7` 表示根为 3，左右子为 9 和 20，
9 无子节点，20 的子节点为 15 和 7。输出 `3 29 22`。""",
    inputs=["3 9 20 null null 15 7\n", "1 null 2 null 3 null 4\n", "5\n", "1 2 3 4 5 null 6\n", "-1 null -2 null -3\n", "7 3 9 1 null 5 8 null null null null null 2\n"],
    ref_py="""import sys
from collections import deque
toks=sys.stdin.read().split()
if not toks or toks[0]=='null':
    print(); sys.exit()
vals=[None if t=='null' else int(t) for t in toks]
n=len(vals)
ch=[[None,None] for _ in range(n)]
root=0; free=[]; nxt=0
# 建树：按层序给非空节点分配子指针槽
nodes=[i for i in range(n) if vals[i] is not None]
q=deque(nodes_map:=[i for i in range(n) if vals[i] is not None])
it=iter(range(1,n))
# 逐个非空节点取接下来两个槽作为左右子
idx=0
qq=deque([0])
pos=1
while qq:
    node=qq.popleft()
    for b in (0,1):
        if pos<n:
            child=pos; pos+=1
            if vals[child] is not None:
                ch[node][b]=child; qq.append(child)
res=[]
qq=deque([0])
while qq:
    sz=len(qq); s=0
    for _ in range(sz):
        u=qq.popleft(); s+=vals[u]
        for c in ch[u]:
            if c is not None: qq.append(c)
    res.append(str(s))
print(" ".join(res))
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){std::vector<std::string>t;std::string x;
while(std::cin>>x)t.push_back(x);
long long n=t.size();std::vector<long long>val(n);
std::vector<char>isn(n);std::vector<std::array<long long,2>>ch(n,{-1,-1});
for(long long i=0;i<n;++i){isn[i]=(t[i]=="null");if(!isn[i])val[i]=std::stoll(t[i]);}
std::deque<long long>q;long long root=-1;
for(long long i=0;i<n;++i)if(!isn[i]){root=i;break;}
if(root==-1){std::cout<<std::endl;return 0;}
q.push_back(root);long long pos=0;
// 按层序为每个非空节点挂接下来的两个槽
std::deque<long long>build;build.push_back(root);
long long slot=0;
for(long long i=0;i<n&&i!=1;i++);
// 直接重用一遍：pos 指向下一个待挂槽位（跳过根）
slot=1;std::deque<long long>bq;bq.push_back(root);
while(!bq.empty()){long long u=bq.front();bq.pop_front();
for(int b=0;b<2&&slot<n;++b,++slot){if(!isn[slot]){ch[u][b]=slot;bq.push_back(slot);}}}
q.clear();q.push_back(root);bool first=true;
while(!q.empty()){long long sz=q.size();long long s=0;
for(long long i=0;i<sz;++i){long long u=q.front();q.pop_front();s+=val[u];
if(ch[u][0]>=0)q.push_back(ch[u][0]);if(ch[u][1]>=0)q.push_back(ch[u][1]);}
if(!first)std::cout<<" ";first=false;std::cout<<s;}
std::cout<<std::endl;}
""",
))

P.append(dict(
    id="cp-009", title="岛屿数量", difficulty="medium", tags=["图", "深度优先搜索", "广度优先搜索"],
    statement="""一个二维网格由 `0`（水）和 `1`（陆地）组成，求岛屿的数量。
岛屿是水平或垂直相邻的陆地连通块（斜向不算相邻）。

**输入格式**：第一行两个整数 m 和 n（1 ≤ m, n ≤ 500）；
接下来 m 行每行 n 个字符（0 或 1，无空格）。

**输出格式**：一个整数，岛屿数量。""",
    inputs=["4 5\n11110\n11010\n11000\n00000\n", "4 5\n11000\n11000\n00100\n00011\n", "1 1\n0\n", "1 1\n1\n", "3 3\n101\n010\n101\n", "3 3\n111\n111\n111\n", "5 5\n10000\n01110\n01110\n01110\n00001\n"],
    ref_py="""import sys
sys.setrecursionlimit(300000)
def main():
    m,n=map(int,input().split())
    g=[input().strip() for _ in range(m)]
    seen=[[False]*n for _ in range(m)]
    cnt=0
    for i in range(m):
        for j in range(n):
            if g[i][j]=='1' and not seen[i][j]:
                cnt+=1
                st=[(i,j)];seen[i][j]=True
                while st:
                    x,y=st.pop()
                    for dx,dy in((1,0),(-1,0),(0,1),(0,-1)):
                        nx,ny=x+dx,y+dy
                        if 0<=nx<m and 0<=ny<n and g[nx][ny]=='1' and not seen[nx][ny]:
                            seen[nx][ny]=True;st.append((nx,ny))
    print(cnt)
main()
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long m,n;std::cin>>m>>n;std::vector<std::string>g(m);
for(auto&row:g)std::cin>>row;std::vector<std::vector<char>>seen(m,std::vector<char>(n,0));
long long cnt=0;std::deque<std::pair<long long,long long>>q;
for(long long i=0;i<m;++i)for(long long j=0;j<n;++j){
if(g[i][j]=='1'&&!seen[i][j]){++cnt;q.clear();q.push_back({i,j});seen[i][j]=1;
while(!q.empty()){auto[x,y]=q.front();q.pop_front();
long long dx[4]={1,-1,0,0},dy[4]={0,0,1,-1};
for(int d=0;d<4;++d){long long nx=x+dx[d],ny=y+dy[d];
if(nx>=0&&nx<m&&ny>=0&&ny<n&&g[nx][ny]=='1'&&!seen[nx][ny]){seen[nx][ny]=1;q.push_back({nx,ny});}}}}}
std::cout<<cnt<<std::endl;}
""",
))

P.append(dict(
    id="cp-010", title="合并区间", difficulty="medium", tags=["排序", "区间"],
    statement="""给定 n 个闭区间 [l, r]，把重叠或相邻（端点相接）的区间合并，
输出合并后的区间集合。

**输入格式**：第一行整数 n（1 ≤ n ≤ 10^5）；接下来 n 行每行两个整数 l r（|l|,|r| ≤ 10^9，l ≤ r）。

**输出格式**：每行一个合并后的区间 `l r`，按左端点升序。""",
    inputs=["4\n1 3\n2 6\n8 10\n15 18\n", "2\n1 4\n4 5\n", "3\n1 2\n3 4\n5 6\n", "1\n-1000000000 1000000000\n", "5\n2 3\n1 2\n5 6\n4 5\n7 8\n", "3\n1 1\n1 1\n0 0\n"],
    ref_py="""n=int(input());iv=[]
for _ in range(n):
    l,r=map(int,input().split());iv.append((l,r))
iv.sort()
out=[]
for l,r in iv:
    if out and l<=out[-1][1]: out[-1][1]=max(out[-1][1],r)
    else: out.append([l,r])
print("\\n".join(f"{l} {r}" for l,r in out))
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n;std::cin>>n;std::vector<std::pair<long long,long long>>iv(n);
for(auto&p:iv)std::cin>>p.first>>p.second;
std::sort(iv.begin(),iv.end());std::vector<std::pair<long long,long long>>out;
for(auto[l,r]:iv){
if(!out.empty()&&l<=out.back().second)out.back().second=std::max(out.back().second,r);
else out.push_back({l,r});}
for(auto[l,r]:out)std::cout<<l<<" "<<r<<"\\n";}
""",
))

P.append(dict(
    id="cp-011", title="LRU 缓存", difficulty="medium", tags=["设计", "哈希表", "双向链表"],
    statement="""设计一个 LRU（最近最少使用）缓存，支持两种操作：
- `set k v`：写入键值；若键已存在则更新值并变为最近使用。
- `get k`：查询；命中返回值并变为最近使用，未命中返回 -1。
容量满时 `set` 淘汰最久未使用的键。
（建议用哈希表 + 双向链表实现 O(1)；直接用 OrderedDict 会被面试官追问实现细节。）

**输入格式**：第一行两个整数 capacity 和 m（1 ≤ capacity ≤ 100，1 ≤ m ≤ 10^5）；
接下来 m 行，每行一个操作。

**输出格式**：每个 `get` 操作输出一行结果。""",
    inputs=["2 9\nset 1 1\nset 2 2\nget 1\nset 3 3\nget 2\nset 4 4\nget 1\nget 3\nget 4\n", "1 4\nset 5 100\nget 5\nset 6 200\nget 5\n", "2 6\nset 1 10\nset 1 20\nget 1\nset 2 0\nset 3 30\nget 1\n", "3 3\nget 9\nget 8\nget 7\n"],
    ref_py="""import sys
cap,m=map(int,input().split())
class Node:
    __slots__=('k','v','prev','nxt')
d={}
head=Node();tail=Node();head.nxt=tail;tail.prev=head
def rm(x):
    x.prev.nxt=x.nxt;x.nxt.prev=x.prev
def front(x):
    x.nxt=head.nxt;x.prev=head;head.nxt.prev=x;head.nxt=x
out=[]
for line in sys.stdin:
    parts=line.split()
    if not parts: continue
    if parts[0]=='set':
        k,v=int(parts[1]),int(parts[2])
        if k in d:
            d[k].v=v;rm(d[k]);front(d[k])
        else:
            if len(d)==cap:
                old=tail.prev;rm(old);del d[old.k]
            x=Node();x.k=k;x.v=v;d[k]=x;front(x)
    else:
        k=int(parts[1])
        if k in d:
            x=d[k];rm(x);front(x);out.append(str(x.v))
        else: out.append("-1")
print("\\n".join(out))
""",
    ref_cpp="""#include <bits/stdc++.h>
struct Node{long long k,v;Node*p,*n;};
int main(){long long cap,m;std::cin>>cap>>m;
std::unordered_map<long long,Node*>d;Node*head=new Node();Node*tail=new Node();
head->n=tail;tail->p=head;
auto rm=[](Node*x){x->p->n=x->n;x->n->p=x->p;};
auto front=[&](Node*x){x->n=head->n;x->p=head;head->n->p=x;head->n=x;};
while(m--){std::string op;long long k;std::cin>>op>>k;
if(op=="set"){long long v;std::cin>>v;
auto it=d.find(k);
if(it!=d.end()){it->second->v=v;rm(it->second);front(it->second);}
else{if((long long)d.size()==cap){Node*old=tail->p;rm(old);d.erase(old->k);}
Node*x=new Node{k,v,nullptr,nullptr};d[k]=x;front(x);}}
else{auto it=d.find(k);
if(it==d.end())std::cout<<-1<<std::endl;
else{Node*x=it->second;rm(x);front(x);std::cout<<x->v<<std::endl;}}}}
""",
))

P.append(dict(
    id="cp-012", title="快速排序", difficulty="medium", tags=["排序", "分治", "递归"],
    statement="""实现快速排序：对 n 个整数升序排序。
（判题只看结果，但面试要求手写快排：请实现真正的 partition 过程，不要调用库排序。）

**输入格式**：第一行整数 n（1 ≤ n ≤ 10^5）；第二行 n 个整数（绝对值 ≤ 10^9）。

**输出格式**：一行，升序序列，空格分隔。""",
    inputs=["5\n5 2 9 1 7\n", "1\n42\n", "6\n3 3 3 1 1 2\n", "8\n-5 10 -20 0 7 -1 3 3\n", "7\n9 8 7 6 5 4 3\n", "4\n1000000000 -1000000000 0 999999999\n"],
    ref_py="""import sys
def qs(a):
    if len(a)<=1: return a
    p=a[len(a)//2]
    l=[x for x in a if x<p];m=[x for x in a if x==p];r=[x for x in a if x>p]
    return qs(l)+m+qs(r)
data=sys.stdin.read().split()
n=int(data[0]);a=list(map(int,data[1:1+n]))
print(" ".join(map(str,qs(a))))
""",
    ref_cpp="""#include <bits/stdc++.h>
void qs(std::vector<long long>&a,long long lo,long long hi){
if(lo>=hi)return;long long p=a[(lo+hi)/2];long long i=lo,j=hi;
while(i<=j){while(a[i]<p)++i;while(a[j]>p)--j;
if(i<=j){std::swap(a[i],a[j]);++i;--j;}}
qs(a,lo,j);qs(a,i,hi);}
int main(){long long n;std::cin>>n;std::vector<long long>a(n);
for(auto&x:a)std::cin>>x;qs(a,0,n-1);
for(long long i=0;i<n;++i)std::cout<<a[i]<<(i+1<n?" ":"\\n");}
""",
))

P.append(dict(
    id="cp-013", title="爬楼梯", difficulty="easy", tags=["动态规划"],
    statement="""你在爬一个 n 阶楼梯，每次可以上 1 阶或 2 阶，求总方案数。
（提示：结果可能很大，n ≤ 60，用 64 位整数。）

**输入格式**：一个整数 n（1 ≤ n ≤ 60）。

**输出格式**：一个整数，方案数。""",
    inputs=["2\n", "3\n", "1\n", "10\n", "30\n", "60\n", "45\n"],
    ref_py="""n=int(input())
a,b=1,1
for _ in range(n-1):a,b=b,a+b
print(a)
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n;std::cin>>n;unsigned long long a=1,b=1;
for(long long i=1;i<n;++i){unsigned long long t=a+b;a=b;b=t;}
std::cout<<a<<std::endl;}
""",
))

P.append(dict(
    id="cp-014", title="零钱兑换", difficulty="medium", tags=["动态规划", "完全背包"],
    statement="""给定 n 种面额的硬币和一个总金额 amount，
每种硬币数量无限，求凑出 amount 的最少硬币数；无法凑出输出 -1。

**输入格式**：第一行两个整数 n 和 amount（1 ≤ n ≤ 20，0 ≤ amount ≤ 10^4）；
第二行 n 个互不相同的正整数面额。

**输出格式**：一个整数，最少硬币数或 -1。""",
    inputs=["3 11\n1 2 5\n", "1 0\n2\n", "2 3\n2 5\n", "4 6418\n1 5 10 25\n", "1 10000\n1\n", "3 7\n2 4 6\n"],
    ref_py="""n,amt=map(int,input().split());c=list(map(int,input().split()))
INF=float('inf')
dp=[0]+[INF]*amt
for x in c:
    for v in range(x,amt+1):
        if dp[v-x]+1<dp[v]: dp[v]=dp[v-x]+1
print(dp[amt] if dp[amt]<INF else -1)
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n,amt;std::cin>>n>>amt;std::vector<long long>c(n);
for(auto&x:c)std::cin>>x;std::vector<long long>dp(amt+1,INT64_MAX/2);dp[0]=0;
for(auto x:c)for(long long v=x;v<=amt;++v)dp[v]=std::min(dp[v],dp[v-x]+1);
if(dp[amt]>=INT64_MAX/2)std::cout<<-1<<std::endl;else std::cout<<dp[amt]<<std::endl;}
""",
))

P.append(dict(
    id="cp-015", title="二叉树的最大深度", difficulty="easy", tags=["二叉树", "递归"],
    statement="""一棵二叉树以层序遍历序列给出（缺失子节点用 `null` 占位，格式同「二叉树每层节点和」题），
求它的最大深度（根深度为 1；空树深度为 0）。

**输入格式**：一行，层序序列。

**输出格式**：一个整数，最大深度。""",
    inputs=["3 9 20 null null 15 7\n", "1 null 2 null 3 null 4\n", "5\n", "1 2 3 4 5 null 6\n", "1 null 2 3 null null 4 5\n", "7 3 9 1 null 5 8 null null null null null 2\n"],
    ref_py="""import sys
from collections import deque
toks=sys.stdin.read().split()
n=len(toks)
if n==0 or toks[0]=='null': print(0); sys.exit()
isn=[t=='null' for t in toks]
ch=[[-1,-1] for _ in range(n)]
root=0;slot=1;bq=deque([root])
while bq:
    u=bq.popleft()
    for b in (0,1):
        if slot<n:
            if not isn[slot]: ch[u][b]=slot; bq.append(slot)
            slot+=1
q=deque([(0,1)]);dep=0
while q:
    u,d=q.popleft();dep=max(dep,d)
    for c in ch[u]:
        if c>=0: q.append((c,d+1))
print(dep)
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){std::vector<std::string>t;std::string x;
while(std::cin>>x)t.push_back(x);long long n=t.size();
if(n==0||t[0]=="null"){std::cout<<0<<std::endl;return 0;}
std::vector<char>isn(n);for(long long i=0;i<n;++i)isn[i]=(t[i]=="null");
std::vector<std::array<long long,2>>ch(n,{-1,-1});
long long slot=1;std::deque<long long>bq;bq.push_back(0);
while(!bq.empty()){long long u=bq.front();bq.pop_front();
for(int b=0;b<2&&slot<n;++b,++slot){if(!isn[slot]){ch[u][b]=slot;bq.push_back(slot);}}}
std::deque<std::pair<long long,long long>>q;q.push_back({0,1});long long dep=0;
while(!q.empty()){auto[u,d]=q.front();q.pop_front();dep=std::max(dep,d);
if(ch[u][0]>=0)q.push_back({ch[u][0],d+1});
if(ch[u][1]>=0)q.push_back({ch[u][1],d+1});}
std::cout<<dep<<std::endl;}
""",
))

P.append(dict(
    id="cp-016", title="移动零", difficulty="easy", tags=["数组", "双指针"],
    statement="""给定一个整数数组，把所有 0 移动到末尾，同时保持非零元素的相对顺序。
要求原地操作（不复制整个数组）。

**输入格式**：第一行整数 n（1 ≤ n ≤ 10^5）；第二行 n 个整数。

**输出格式**：一行，处理后的序列，空格分隔。""",
    inputs=["5\n0 1 0 3 12\n", "1\n0\n", "4\n1 2 3 4\n", "6\n0 0 0 0 0 1\n", "3\n7 0 0\n", "5\n0 0 1 0 2\n"],
    ref_py="""n=int(input());a=input().split()
slow=0
for fast in range(n):
    if a[fast]!='0':
        a[slow],a[fast]=a[fast],a[slow];slow+=1
print(" ".join(a))
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n;std::cin>>n;std::vector<std::string>a(n);
for(auto&x:a)std::cin>>x;long long slow=0;
for(long long fast=0;fast<n;++fast){if(a[fast]!="0"){
if(fast!=slow)std::swap(a[slow],a[fast]);++slow;}}
for(long long i=0;i<n;++i)std::cout<<a[i]<<(i+1<n?" ":"\\n");}
""",
))

P.append(dict(
    id="cp-017", title="课程修读顺序（拓扑排序）", difficulty="medium", tags=["图", "拓扑排序"],
    statement="""共有 numCourses 门课，编号 1..numCourses，给定 m 条依赖关系 `a b` 表示修 a 之前必须先修 b。
求一个可行的修读顺序；若存在环输出 -1。
若有多个可行顺序，输出 **字典序最小** 的那个（用最小堆实现拓扑排序）。

**输入格式**：第一行两个整数 numCourses 和 m（1 ≤ numCourses ≤ 10^5, 0 ≤ m ≤ 2×10^5）；
接下来 m 行每行 `a b`。

**输出格式**：一行，numCourses 个整数（可行的修读顺序）或 -1。""",
    inputs=["4 3\n2 1\n3 1\n4 2\n", "2 2\n1 2\n2 1\n", "3 0\n", "5 4\n2 1\n3 1\n4 3\n5 3\n", "1 0\n", "3 2\n3 1\n3 2\n", "6 6\n2 1\n3 1\n4 2\n5 2\n6 3\n6 5\n"],
    ref_py="""import heapq
N,M=map(int,input().split())
adj=[[] for _ in range(N+1)];ind=[0]*(N+1)
for _ in range(M):
    a,b=map(int,input().split())
    adj[b].append(a);ind[a]+=1
h=[i for i in range(1,N+1) if ind[i]==0]
heapq.heapify(h)
out=[]
while h:
    u=heapq.heappop(h);out.append(u)
    for v in adj[u]:
        ind[v]-=1
        if ind[v]==0: heapq.heappush(h,v)
print(" ".join(map(str,out)) if len(out)==N else -1)
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long N,M;std::cin>>N>>M;
std::vector<std::vector<long long>>adj(N+1);std::vector<long long>ind(N+1,0);
for(long long i=0;i<M;++i){long long a,b;std::cin>>a>>b;adj[b].push_back(a);ind[a]++;}
std::priority_queue<long long,std::vector<long long>,std::greater<>>h;
for(long long i=1;i<=N;++i)if(ind[i]==0)h.push(i);
std::vector<long long>out;
while(!h.empty()){long long u=h.top();h.pop();out.push_back(u);
for(auto v:adj[u])if(--ind[v]==0)h.push(v);}
if((long long)out.size()!=N)std::cout<<-1<<std::endl;
else{for(long long i=0;i<N;++i)std::cout<<out[i]<<(i+1<N?" ":"\\n");}}
""",
))

P.append(dict(
    id="cp-018", title="前 K 个高频元素", difficulty="medium", tags=["哈希表", "堆"],
    statement="""给定整数数组和 k，输出出现频率前 k 高的元素。
频率相同时元素值小的排在前面。

**输入格式**：第一行两个整数 n 和 k（1 ≤ n ≤ 10^5, 1 ≤ k ≤ 不同元素数）；
第二行 n 个整数。

**输出格式**：一行 k 个整数，空格分隔。""",
    inputs=["6 2\n1 1 1 2 2 3\n", "1 1\n1\n", "8 3\n4 4 4 6 6 7 7 7\n", "5 3\n5 -3 5 -3 9\n", "7 2\n1 2 3 1 2 3 3\n", "4 4\n9 8 7 6\n"],
    ref_py="""from collections import Counter
n,k=map(int,input().split());a=list(map(int,input().split()))
c=Counter(a)
items=sorted(c.items(),key=lambda kv:(-kv[1],kv[0]))
print(" ".join(str(x) for x,_ in items[:k]))
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long n,k;std::cin>>n>>k;std::unordered_map<long long,long long>c;
for(long long i=0;i<n;++i){long long x;std::cin>>x;++c[x];}
std::vector<std::pair<long long,long long>>v(c.begin(),c.end());
std::sort(v.begin(),v.end(),[](auto&a,auto&b){
return a.second!=b.second?a.second>b.second:a.first<b.first;});
for(long long i=0;i<k;++i)std::cout<<v[i].first<<(i+1<k?" ":"\\n");}
""",
))

P.append(dict(
    id="cp-019", title="最长回文子串", difficulty="medium", tags=["字符串", "动态规划", "双指针"],
    statement="""给定一个字符串，求它的最长回文子串；若有多个长度相同的，输出最靠左的那个。

**输入格式**：一行字符串（长度 ≤ 2000，可含大小写字母与数字）。

**输出格式**：一行，最长回文子串。""",
    inputs=["babad\n", "cbbd\n", "a\n", "bananas\n", "abcde\n", "forgeeksskeegfor\n", "aaaa\n", "abacdfgdcaba\n"],
    ref_py="""s=input().strip()
n=len(s)
if n==0: print()
else:
    best=(0,0)
    for c in range(n):
        for l,r in ((c,c),(c,c+1)):
            while l>=0 and r<n and s[l]==s[r]: l-=1;r+=1
            l+=1;r-=1
            if r-l>best[1]-best[0] or (r-l==best[1]-best[0] and l<best[0]):
                best=(l,r)
    print(s[best[0]:best[1]+1])
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){std::string s;std::cin>>s;long long n=s.size();
if(n==0){std::cout<<std::endl;return 0;}
long long bl=0,br=0;
for(long long c=0;c<n;++c){for(int t=0;t<2;++t){
long long l=c,r=c+t;
while(l>=0&&r<n&&s[l]==s[r]){--l;++r;}
++l;--r;
if(r-l>br-bl||(r-l==br-bl&&l<bl)){bl=l;br=r;}}}
std::cout<<s.substr(bl,br-bl+1)<<std::endl;}
""",
))

P.append(dict(
    id="cp-020", title="迷宫最短步数", difficulty="medium", tags=["图", "广度优先搜索"],
    statement="""一个 m×n 的迷宫，`.` 为通路、`#` 为墙、`S` 为起点、`E` 为终点。
每步可上下左右移动一格（不能穿墙），求从 S 到 E 的最少步数；不可达输出 -1。

**输入格式**：第一行两个整数 m 和 n（1 ≤ m, n ≤ 1000）；
接下来 m 行每行 n 个字符。

**输出格式**：一个整数，最少步数或 -1。""",
    inputs=["3 3\nS.E\n.#.\n...\n", "3 4\nS#.E\n.#..\n....\n", "2 2\nSE\n##\n", "1 5\nS..#E\n", "5 5\nS....\n.###.\n.#E#.\n.#...\n.....\n", "3 3\nS#E\n.#.\n...\n"],
    ref_py="""from collections import deque
m,n=map(int,input().split())
g=[input().strip() for _ in range(m)]
sx=sy=ex=ey=-1
for i in range(m):
    for j in range(n):
        if g[i][j]=='S': sx,sy=i,j
        elif g[i][j]=='E': ex,ey=i,j
dist=[[-1]*n for _ in range(m)]
dist[sx][sy]=0
q=deque([(sx,sy)])
while q:
    x,y=q.popleft()
    if (x,y)==(ex,ey): break
    for dx,dy in((1,0),(-1,0),(0,1),(0,-1)):
        nx,ny=x+dx,y+dy
        if 0<=nx<m and 0<=ny<n and g[nx][ny]!='#' and dist[nx][ny]==-1:
            dist[nx][ny]=dist[x][y]+1;q.append((nx,ny))
print(dist[ex][ey])
""",
    ref_cpp="""#include <bits/stdc++.h>
int main(){long long m,n;std::cin>>m>>n;std::vector<std::string>g(m);
for(auto&r:g)std::cin>>r;
long long sx=-1,sy=-1,ex=-1,ey=-1;
for(long long i=0;i<m;++i)for(long long j=0;j<n;++j){
if(g[i][j]=='S'){sx=i;sy=j;}else if(g[i][j]=='E'){ex=i;ey=j;}}
std::vector<std::vector<long long>>dist(m,std::vector<long long>(n,-1));
std::deque<std::pair<long long,long long>>q;dist[sx][sy]=0;q.push_back({sx,sy});
long long dx[4]={1,-1,0,0},dy[4]={0,0,1,-1};
while(!q.empty()){auto[x,y]=q.front();q.pop_front();
for(int d=0;d<4;++d){long long nx=x+dx[d],ny=y+dy[d];
if(nx>=0&&nx<m&&ny>=0&&ny<n&&g[nx][ny]!='#'&&dist[nx][ny]==-1){
dist[nx][ny]=dist[x][y]+1;q.push_back({nx,ny});}}}
std::cout<<dist[ex][ey]<<std::endl;}
""",
))



SAMPLE_EXPLAIN = {
    "cp-001": "样例输入第一行 \"4 9\"：4 是数组长度 n，9 是目标值 target；第二行 \"2 7 11 15\" 是这 4 个升序整数。因为 2 + 7 = 9，这两个数在第 1、2 个位置，所以输出 \"1 2\"（下标从 1 开始，小的在前）。",
    "cp-002": "输入字符串 abcabcbb 中，最长的不含重复字符的连续子串是 abc，长度为 3。",
    "cp-003": "9 个整数中，和最大的连续子数组是 [4, -1, 2, 1]，和为 6。注意子数组必须连续，不能跳着选。",
    "cp-004": "n=5、k=2，序列 1 2 3 4 5 按每 2 个一组分组：(1 2)(3 4)(5)，组内翻转得到 2 1 4 3 5；最后一组 (5) 不足 2 个，保持原序。",
    "cp-005": "第一行 \"5 3\"：数组有 5 个数，要查询 3 次；第二行是升序数组；第三行 \"3 6 1\" 是 3 个查询。3 第一次出现在第 2 个位置→输出 2；6 不存在→-1；1 在第 1 个位置→1。所以输出 \"2 -1 1\"。",
    "cp-006": "每个左括号都按正确顺序被同类型右括号闭合，所以输出 valid。",
    "cp-007": "n=8、k=3，窗口从左往右滑动共 6 个位置，每个窗口 3 个数的最大值依次是 3 3 5 5 6 7。",
    "cp-008": "这棵树的根是 3；9 和 20 是第 2 层（9 的两个子节点用 null 占位）；15 和 7 是第 3 层。三层各自的和是 3、29、22。",
    "cp-009": "4 行 5 列的网格中，所有 1 组成一个连通块（左上角区域），所以岛屿数为 1。上下左右相邻的 1 属于同一座岛，斜向不算。",
    "cp-010": "区间 [1,3] 与 [2,6] 重叠，合并为 [1,6]；[8,10] 和 [15,18] 与其他区间不相连，各自保留。",
    "cp-011": "容量为 2。set 1、set 2 后缓存为 {1,2}；get 1 命中输出 1（1 变为最近使用）；set 3 时缓存已满，淘汰最久未用的 2；get 2 输出 -1；set 4 又淘汰 1；最终 get 1 输出 -1、get 3 输出 3、get 4 输出 4。",
    "cp-012": "把 5 2 9 1 7 升序排列，输出 1 2 5 7 9。",
    "cp-013": "2 阶楼梯有 2 种爬法：1+1 或直接 2 阶。",
    "cp-014": "面额 {1, 2, 5}，凑出 11 最少用 3 枚硬币：5 + 5 + 1。",
    "cp-015": "这棵树共 3 层（根为第 1 层），所以深度为 3。",
    "cp-016": "把 0 1 0 3 12 中的 0 全部移到末尾，非零元素 1 3 12 保持相对顺序，输出 1 3 12 0 0。",
    "cp-017": "依赖 \"2 1\" 表示修 2 之前必须先修 1。课程 1 无前置先修；之后 2、3 都被解锁，字典序取 2；2 修完解锁 4。可行且字典序最小的顺序是 1 2 3 4。",
    "cp-018": "数组 1 1 1 2 2 3 中，1 出现 3 次、2 出现 2 次、3 出现 1 次，频率前 2 高的是 1 和 2。",
    "cp-019": "babad 的最长回文子串是 bab（长度 3；aba 同为 3 但更靠右，按要求取最靠左的）。",
    "cp-020": "S 在左上角，E 在同一行右侧，中间是通路，向右走 2 步即达，所以最短步数为 2。",
}

def main() -> int:
    out_dir = REPO / "seeds" / "coding"
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    for p in P:
        cases = []
        for i, inp in enumerate(p["inputs"]):
            res = judge_submission(p["ref_py"], "python", [{"input": inp, "output": ""}])
            if res.verdict != "AC" and res.verdict != "WA":
                failures.append((p["id"], "python", res.verdict, res.cases[0].stderr[:200]))
                continue
            got = res.cases[0].stdout
            # 交叉验证 C++ 参考解
            resc = judge_submission(p["ref_cpp"], "cpp", [{"input": inp, "output": got}])
            if resc.verdict != "AC":
                failures.append((p["id"], "cpp", resc.verdict, resc.cases[0].stderr[:200] if resc.cases else resc.compile_error[:200]))
                continue
            cases.append({"input": inp, "output": got.rstrip("\n"), "sample": i == 0})
        if len(cases) != len(p["inputs"]):
            continue
        statement = p["statement"]
        if p["id"] in SAMPLE_EXPLAIN:
            statement += "\n\n【样例解释】\n" + SAMPLE_EXPLAIN[p["id"]]
        leetcode_id, interview_priority = INTERVIEW_META[p["id"]]
        obj = {
            "id": p["id"], "title": p["title"], "difficulty": p["difficulty"],
            "leetcode_id": leetcode_id,
            "interview_priority": interview_priority,
            "tags": p["tags"], "statement": statement,
            "languages": ["python", "cpp"],
            "test_cases": cases,
            "reference": {"python": p["ref_py"], "cpp": p["ref_cpp"]},
        }
        (out_dir / f"{p['id']}.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[ok] {p['id']} {p['title']} ({len(cases)} 用例)")
    if failures:
        print("\n[失败]")
        for f in failures:
            print(" ", f)
        return 1
    print(f"\n共生成 {len(P)} 道种子题 → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
