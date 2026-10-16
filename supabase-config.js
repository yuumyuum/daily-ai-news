// Supabase 연결 정보 (찬반 투표용)
// Supabase 대시보드 > Project Settings > API 에서 복사해 넣으세요.
//   url     : Project URL           (예: https://abcdefghijkl.supabase.co)
//   anonKey : Publishable key(sb_publishable_...) 또는 Legacy 탭의 anon public 키(eyJ...)
//             둘 다 공개용 키라 웹페이지에 넣어도 됩니다.
// ※ secret / service_role 키는 절대 넣지 마세요. 모든 학생 정보를 읽을 수 있는 관리자 키입니다.
// 비워 두면 투표 영역에 "준비 중" 안내만 표시됩니다.
window.SUPABASE_CONFIG = {
  url: 'https://pnytvmpwpruobwqfqctf.supabase.co',
  anonKey: 'sb_publishable_rXCLAOiBwsLqicQ-tTBNFA_QM-c6tEP'
};
