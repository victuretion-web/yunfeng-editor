import os
import csv
import glob
import sys

def main():
    print("=" * 60)
    print("字幕同步率检测 (CI Check)")
    print("=" * 60)
    
    output_dir = os.path.join(os.path.dirname(__file__), "output")
    csv_files = glob.glob(os.path.join(output_dir, "*_字幕对齐报告.csv"))
    
    if not csv_files:
        print("[!] 未找到任何字幕对齐报告，跳过检测。")
        return
        
    total_lines = 0
    synced_lines = 0
    total_offset_ms = 0
    
    for file in csv_files:
        filename = os.path.basename(file)
        with open(file, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            lines = list(reader)
            
            file_total = len(lines)
            file_synced = 0
            
            for row in lines:
                offset = float(row.get('偏移误差(ms)', 0))
                total_offset_ms += abs(offset)
                if abs(offset) <= 100:
                    file_synced += 1
            
            total_lines += file_total
            synced_lines += file_synced
            
            sync_rate = (file_synced / file_total * 100) if file_total > 0 else 0
            print(f"📄 {filename}")
            print(f"   - 总字幕数: {file_total}")
            print(f"   - 达标行数(±100ms): {file_synced}")
            print(f"   - 同步率: {sync_rate:.2f}%")
            print("-" * 60)
            
    if total_lines == 0:
        print("[!] 报告中无字幕数据。")
        return
        
    overall_sync_rate = (synced_lines / total_lines) * 100
    avg_offset = total_offset_ms / total_lines
    
    print("📊 最终汇总指标")
    print(f"   - 总解析字幕: {total_lines}")
    print(f"   - 平均偏移误差: {avg_offset:.2f} ms")
    print(f"   - 全局同步率: {overall_sync_rate:.2f}%")
    
    if overall_sync_rate >= 98.0:
        print("\n✅ 测试通过: 同步率 >= 98%")
        sys.exit(0)
    else:
        print("\n❌ 测试失败: 同步率 < 98%")
        sys.exit(1)

if __name__ == "__main__":
    main()
